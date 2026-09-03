from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from bson import ObjectId
from datetime import datetime
from db import db
from sales_close_types import typed_inc
from services.activation_groups import get_accessible_agent_ids
from services.loans import money, mongo_money
from services.payment_messages import render_payment_message
from services.sms_gateway import normalize_ghana_phone, send_sms_detailed

payment_bp = Blueprint('payment', __name__)

customers_collection = db["customers"]
payments_collection  = db["payments"]
users_collection     = db["users"]
sales_close_collection = db["sales_close"]  # ✅ NEW: daily rollup per agent
archived_customers_collection = db["Archived_customers"]

def _is_ajax(req) -> bool:
    return req.headers.get("X-Requested-With", "").lower() == "xmlhttprequest"


def _normalize_phone(raw: str) -> str | None:
    if not raw:
        return None
    p = raw.strip().replace(' ', '').replace('-', '').replace('+', '')
    if p.startswith('0') and len(p) == 10:
        p = '233' + p[1:]
    if p.startswith('233') and len(p) == 12:
        return p
    return None


def _money_text(value) -> str:
    try:
        return f"{float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _send_payment_sms(customer: dict, payment_type: str, values: dict) -> tuple[str, str, dict]:
    phone = normalize_ghana_phone(customer.get('phone_number', ''))
    if phone is None:
        return 'invalid_phone', '', {'status': 'invalid_phone', 'provider': 'VireSender'}

    full_name = str(customer.get('name') or 'Customer').strip() or 'Customer'
    message_values = {
        'name': full_name.split()[0],
        'full_name': full_name,
        'payment_type': payment_type.title(),
        **values,
    }
    message = render_payment_message(payment_type, message_values)
    if message is None:
        return 'disabled', '', {'status': 'disabled', 'provider': 'VireSender'}
    delivery = send_sms_detailed(phone, message)
    return str(delivery['status']), message, delivery


def _json_safe(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(val) for key, val in value.items()}
    return value


def _existing_payment_info(customer_id: ObjectId, payment_date_str: str, is_susu: bool, product_index: int | None):
    """
    Look for payments already recorded for the same customer on the same date
    and within the same scope (SUSU vs specific PRODUCT index).
    Returns (count, total, scope_label).
    """
    q = {
        'customer_id': customer_id,
        'date': payment_date_str,
        'payment_type': {'$ne': 'WITHDRAWAL'}  # ignore withdrawals
    }

    scope_label = "SUSU" if is_susu else "PRODUCT"

    if is_susu:
        q['payment_type'] = 'SUSU'
    else:
        q['payment_type'] = 'PRODUCT'
        q['product_index'] = product_index if product_index is not None else -1

    docs = list(payments_collection.find(q))
    count = len(docs)
    total = sum(float(d.get('amount', 0.0)) for d in docs)

    if not is_susu:
        # Try to include product name in label (if any prior payment had it)
        for d in docs:
            name = d.get('product_name')
            if name:
                scope_label = f"PRODUCT: {name}"
                break

    return count, total, scope_label


def _restore_archived_customer(customer_id_raw: str, agent_ids: list[str]):
    """
    If customer exists in Archived_customers for this agent, move back to customers.
    Returns (customer_doc or None, error_message or None)
    """
    candidates = [customer_id_raw]
    try:
        candidates.append(ObjectId(customer_id_raw))
    except Exception:
        pass

    archived = archived_customers_collection.find_one({
        "_id": {"$in": candidates},
        "agent_id": {"$in": agent_ids}
    })
    if not archived:
        return None, "Archived customer not found."

    # Do not overwrite if already exists in active customers
    existing = customers_collection.find_one({"_id": archived.get("_id")})
    if existing:
        return existing, None

    now_utc = datetime.utcnow()
    restored = dict(archived)
    restored.update({
        "restored_at": now_utc,
        "restored_by": str(current_user.id),
        "restored_by_role": "agent",
        "restored_from": "Archived_customers",
        "restored_reason": "Payment initiated by agent"
    })

    try:
        customers_collection.insert_one(restored)
    except Exception:
        return None, "Failed to restore archived customer."

    deleted = archived_customers_collection.delete_one({"_id": archived.get("_id")})
    if not deleted or deleted.deleted_count != 1:
        return None, "Restored customer, but failed to remove from archive."

    return restored, None


@payment_bp.route('/add_payment', methods=['GET', 'POST'])
@login_required
def add_payment():
    today_date = datetime.today().date()

    if request.method == 'POST':
        accessible_agent_ids = get_accessible_agent_ids(current_user.id)
        customer_id = request.form.get('customer_id')
        product_index_raw = request.form.get('product_id')
        method = request.form.get('method') or "Cash"
        amount_raw = request.form.get('amount')
        date_str = today_date.strftime('%Y-%m-%d')
        payment_date = datetime.combine(today_date, datetime.min.time())
        is_susu = request.form.get('is_susu') == 'yes'
        payment_type = (request.form.get('payment_type') or ('SUSU' if is_susu else 'PRODUCT')).upper()
        if payment_type not in {'PRODUCT', 'SUSU', 'LOAN'}:
            if _is_ajax(request):
                return jsonify(ok=False, message='Invalid payment type.'), 400
            flash('Invalid payment type.', 'danger')
            return redirect(url_for('payment.add_payment'))
        is_susu = payment_type == 'SUSU'
        is_loan = payment_type == 'LOAN'
        send_sms = request.form.get('send_sms') == 'yes'
        force_insert = request.form.get('force') == 'yes'

        # ---- Basic validation ----
        if not all([customer_id, amount_raw]):
            if _is_ajax(request):
                return jsonify(ok=False, message='All fields are required!'), 400
            flash('All fields are required!', 'danger')
            return redirect(url_for('payment.add_payment'))

        try:
            amount = float(amount_raw)
        except ValueError:
            if _is_ajax(request):
                return jsonify(ok=False, message='Invalid payment amount.'), 400
            flash('Invalid payment amount.', 'danger')
            return redirect(url_for('payment.add_payment'))

        if amount <= 0:
            if _is_ajax(request):
                return jsonify(ok=False, message='Payment amount must be greater than zero.'), 400
            flash('Payment amount must be greater than zero.', 'danger')
            return redirect(url_for('payment.add_payment'))

        # ---- Auth + entities ----
        try:
            cust_oid = ObjectId(customer_id)
        except Exception:
            if _is_ajax(request):
                return jsonify(ok=False, message='Invalid customer id.'), 400
            flash('Invalid customer.', 'danger')
            return redirect(url_for('payment.add_payment'))

        customer = customers_collection.find_one({
            '_id': cust_oid,
            'agent_id': {'$in': accessible_agent_ids}
        })

        # If not active, try restore from archive on payment initiation
        if not customer:
            restored, err = _restore_archived_customer(customer_id, accessible_agent_ids)
            if err:
                if _is_ajax(request):
                    return jsonify(ok=False, message=err), 403
                flash(err, 'danger')
                return redirect(url_for('payment.add_payment'))
            customer = restored

        if not customer:
            if _is_ajax(request):
                return jsonify(ok=False, message='Unauthorized or customer not found.'), 403
            flash('Unauthorized access or customer not found.', 'danger')
            return redirect(url_for('payment.add_payment'))

        agent = users_collection.find_one({
            "_id": ObjectId(current_user.id),
            "role": "agent"
        })

        if not agent or "manager_id" not in agent:
            if _is_ajax(request):
                return jsonify(ok=False, message='Agent not linked to a manager. Contact admin.'), 400
            flash('Agent not linked to a manager. Contact admin.', 'danger')
            return redirect(url_for('payment.add_payment'))

        owner_agent_id = str(customer.get('agent_id') or current_user.id)
        owner_manager_id = customer.get('manager_id') or agent.get('manager_id')
        if owner_manager_id is None:
            if _is_ajax(request):
                return jsonify(ok=False, message='Customer owner is not linked to a manager.'), 400
            flash('Customer owner is not linked to a manager.', 'danger')
            return redirect(url_for('payment.add_payment'))

        # ---- Loan repayment scope ----
        if is_loan:
            from routes.loans import active_loan_for_customer, sync_loan
            selected_loan_id = request.form.get('loan_id')
            if not selected_loan_id:
                if _is_ajax(request):
                    return jsonify(ok=False, message='Select the loan receiving this payment.'), 400
                flash('Select the loan receiving this payment.', 'danger')
                return redirect(url_for('payment.add_payment'))
            loan = active_loan_for_customer(cust_oid, selected_loan_id)
            if not loan or str(loan.get('agent_id')) not in accessible_agent_ids:
                if _is_ajax(request):
                    return jsonify(ok=False, message='No active loan found for this customer.'), 404
                flash('No active loan found for this customer.', 'danger')
                return redirect(url_for('payment.add_payment'))
            current_balance = money(loan.get('current_balance'))
            pay_amount = money(amount_raw)
            if pay_amount > current_balance:
                message = f'Payment exceeds the loan balance of GHS {current_balance:.2f}.'
                if _is_ajax(request):
                    return jsonify(ok=False, message=message), 400
                flash(message, 'danger')
                return redirect(url_for('payment.add_payment'))
            now_utc = datetime.utcnow()
            new_paid = money(loan.get('amount_paid')) + pay_amount
            new_balance = current_balance - pay_amount
            new_status = 'settled' if new_balance == 0 else loan.get('status', 'active')
            updated = db.loans.update_one(
                {'_id': loan['_id'], 'current_balance': loan.get('current_balance'), 'status': {'$in': ['active','approved','grace_period','overdue']}},
                {'$set': {'amount_paid': mongo_money(new_paid), 'current_balance': mongo_money(new_balance),
                          'status': new_status, 'updated_at': now_utc,
                          **({'settled_at': now_utc} if new_status == 'settled' else {})}}
            )
            if updated.modified_count != 1:
                if _is_ajax(request):
                    return jsonify(ok=False, message='Loan changed while saving. Please retry.'), 409
                flash('Loan changed while saving. Please retry.', 'warning')
                return redirect(url_for('payment.add_payment'))
            payment_result = payments_collection.insert_one({
                'customer_id': cust_oid, 'loan_id': loan['_id'], 'loan_number': loan.get('loan_number'),
                'agent_id': owner_agent_id, 'recorded_by_agent_id': str(current_user.id), 'manager_id': owner_manager_id,
                'method': method, 'amount': float(pay_amount), 'date': payment_date.strftime('%Y-%m-%d'),
                'time': now_utc.strftime('%H:%M:%S'), 'payment_type': 'LOAN', 'created_at': now_utc
            })
            sales_close_collection.update_one(
                {'agent_id': owner_agent_id, 'date': date_str},
                {
                    '$setOnInsert': {
                        'agent_id': owner_agent_id, 'manager_id': owner_manager_id,
                        'date': date_str, 'created_at': now_utc,
                    },
                    '$inc': {**typed_inc('LOAN', float(pay_amount)), 'count': 1},
                    '$set': {
                        'last_payment_at': now_utc, 'updated_at': now_utc,
                        'last_recorded_by_agent_id': str(current_user.id),
                    },
                },
                upsert=True,
            )
            sms_status = None
            if send_sms:
                try:
                    sms_status, sms_message, sms_delivery = _send_payment_sms(customer, 'LOAN', {
                        'payment_amount': _money_text(pay_amount),
                        'payment_date': date_str,
                        'loan_number': str(loan.get('loan_number') or ''),
                        'loan_total': _money_text(new_paid + new_balance),
                        'loan_paid': _money_text(new_paid),
                        'loan_amount_left': _money_text(new_balance),
                    })
                except Exception as exc:
                    print("SMS sending error:", str(exc))
                    sms_status, sms_message, sms_delivery = 'error', '', {'status': 'error', 'error': str(exc)}
                payments_collection.update_one(
                    {'_id': payment_result.inserted_id},
                    {'$push': {'sms_events': {**_json_safe(sms_delivery), 'status': sms_status, 'message': sms_message, 'created_at': datetime.utcnow()}}}
                )
            if _is_ajax(request):
                response_message = 'Loan payment recorded successfully.'
                if sms_status == 'sent':
                    response_message = 'Loan payment recorded and SMS sent successfully.'
                elif sms_status in ('failed', 'error', 'not_configured'):
                    response_message = 'Loan payment recorded, but SMS delivery failed.'
                elif sms_status == 'invalid_phone':
                    response_message = 'Loan payment recorded; phone number invalid for SMS.'
                return jsonify(
                    ok=True,
                    message=response_message,
                    loan_paid=float(new_paid),
                    loan_balance=float(new_balance),
                    loan_id=str(loan['_id']),
                )
            flash('Loan payment recorded successfully.', 'success')
            return redirect(url_for('payment.add_payment'))

        # ---- Product scope (if PRODUCT mode) ----
        product_index = None
        product_name = None
        product_total = None
        if not is_susu:
            try:
                product_index = int(product_index_raw)
            except Exception:
                if _is_ajax(request):
                    return jsonify(ok=False, message='Invalid product selected.'), 400
                flash('Invalid product selected.', 'danger')
                return redirect(url_for('payment.add_payment'))

            purchases = customer.get('purchases', [])
            if product_index < 0 or product_index >= len(purchases):
                if _is_ajax(request):
                    return jsonify(ok=False, message='Selected product not found for this customer.'), 404
                flash('Selected product not found for this customer.', 'danger')
                return redirect(url_for('payment.add_payment'))

            sel = purchases[product_index]
            prod = sel.get('product', {})
            product_name = prod.get('name', 'Unnamed Product')
            try:
                product_total = float(prod.get('total', 0))
            except Exception:
                product_total = 0.0

        # ---- Duplicate check (same customer, same date, same scope) ----
        existing_count, existing_total, scope_label = _existing_payment_info(
            cust_oid, payment_date.strftime('%Y-%m-%d'), is_susu, product_index
        )

        if existing_count > 0 and not force_insert:
            # Tell the frontend to confirm override
            if _is_ajax(request):
                return jsonify(
                    ok=False,
                    needs_confirm=True,
                    existing_count=existing_count,
                    existing_total=round(existing_total, 2),
                    scope=scope_label,
                    message=f"You already recorded {existing_count} payment(s) totaling GHS {existing_total:.2f} for this {scope_label} on {date_str}. Proceed anyway?"
                ), 409
            # Non-AJAX fallback: inform via flash (user can re-submit with force=yes)
            flash(f"You already recorded {existing_count} payment(s) totaling GHS {existing_total:.2f} for this {scope_label} on {date_str}. Resubmit to confirm.", 'warning')
            return redirect(url_for('payment.add_payment'))

        # ---- Build payment doc & insert ----
        now_utc = datetime.utcnow()
        time_str = now_utc.strftime('%H:%M:%S')     # ✅ NEW: separate time
        date_only_str = payment_date.strftime('%Y-%m-%d')

        payment_doc = {
            'customer_id': cust_oid,
            'agent_id': owner_agent_id,
            'recorded_by_agent_id': str(current_user.id),
            'manager_id': owner_manager_id,
            'method': method,
            'amount': amount,
            'date': date_only_str,                   # YYYY-MM-DD
            'time': time_str,                        # ✅ NEW: HH:MM:SS
            'payment_type': 'SUSU' if is_susu else 'PRODUCT',
            'created_at': now_utc
        }

        if not is_susu:
            payment_doc.update({
                'product_index': product_index,
                'product_name': product_name,
                'product_total': product_total
            })

        if customer.get('activation'):
            payment_doc.update({
                'activation_id': customer.get('activation_id'),
                'activation_team_name': customer.get('activation_team_name') or 'Activation Team',
                'activation_leader_id': customer.get('activation_leader_id') or owner_agent_id
            })

        payment_result = payments_collection.insert_one(payment_doc)

        # Lead lifecycle: convert lead -> customer on first non-withdrawal payment
        customers_collection.update_one(
            {
                "_id": cust_oid,
                "$or": [{"lead_stage": {"$exists": False}}, {"lead_stage": "lead"}]
            },
            {
                "$set": {
                    "lead_stage": "customer",
                    "lead_converted_at": now_utc
                }
            }
        )

        # ---- NEW: roll-up / daily close per agent in `sales_close` ----
        sales_close_filter = {
            'agent_id': owner_agent_id,
            'date': date_only_str
        }
        sales_close_update = {
            '$setOnInsert': {
                'agent_id': owner_agent_id,
                'manager_id': owner_manager_id,
                'date': date_only_str,
                'created_at': now_utc
            },
            '$inc': {
                **typed_inc('SUSU' if is_susu else 'PRODUCT', amount),
                'count': 1
            },
            '$set': {
                'last_payment_at': now_utc,
                'updated_at': now_utc,
                'last_recorded_by_agent_id': str(current_user.id)
            }
        }
        sales_close_collection.update_one(sales_close_filter, sales_close_update, upsert=True)

        # ---- Optional SMS ----
        sms_status = None
        if send_sms:
            message = ''
            try:
                if is_susu:
                    susu_total = sum(
                        float(p.get('amount', 0))
                        for p in payments_collection.find({'customer_id': cust_oid, 'payment_type': 'SUSU'})
                    )
                    message_values = {
                        'payment_amount': _money_text(amount),
                        'payment_date': date_str,
                        'susu_total': _money_text(susu_total),
                    }
                else:
                    product_payments = payments_collection.find({
                        'customer_id': cust_oid,
                        'product_index': product_index,
                        'payment_type': 'PRODUCT'
                    })
                    product_paid = sum(float(p.get('amount', 0)) for p in product_payments)
                    message_values = {
                        'payment_amount': _money_text(amount),
                        'payment_date': date_str,
                        'product_name': product_name or '',
                        'product_total': _money_text(product_total),
                        'product_paid': _money_text(product_paid),
                        'product_amount_left': _money_text(max(0, float(product_total or 0) - product_paid)),
                    }
                sms_status, message, sms_delivery = _send_payment_sms(
                    customer,
                    'SUSU' if is_susu else 'PRODUCT',
                    message_values,
                )
            except Exception as e:
                print("SMS sending error:", str(e))
                sms_status = 'error'
                sms_delivery = {'status': 'error', 'error': str(e)}

            payments_collection.update_one(
                {'_id': payment_result.inserted_id},
                {'$push': {'sms_events': {**_json_safe(sms_delivery), 'status': sms_status or 'not_sent', 'message': message, 'created_at': datetime.utcnow()}}}
            )

        # ---- Respond (AJAX vs normal) ----
        if _is_ajax(request):
            msg = 'Payment added successfully.'
            if sms_status == 'sent':
                msg = 'Payment added and SMS sent successfully.'
            elif sms_status in ('failed', 'error', 'not_configured'):
                msg = 'Payment added, but SMS delivery failed.'
            elif sms_status == 'invalid_phone':
                msg = 'Payment added; phone number invalid for SMS.'

            return jsonify(ok=True, message=msg)

        # Non-AJAX fallback
        if sms_status == 'sent':
            flash('Payment added and SMS sent successfully.', 'success')
        elif sms_status in ('failed', 'error', 'not_configured'):
            flash('Payment added but SMS delivery failed.', 'warning')
        elif sms_status == 'invalid_phone':
            flash('Payment added; phone number invalid for SMS.', 'warning')
        else:
            flash('Payment added successfully. (SMS not sent)', 'success')

        return redirect(url_for('payment.add_payment'))

    # ---------- GET ----------
    accessible_agent_ids = get_accessible_agent_ids(current_user.id)
    raw_customers = list(customers_collection.find(
        {'agent_id': {'$in': accessible_agent_ids}},
        {'name': 1, 'phone_number': 1, 'purchases': 1, 'image_url': 1}
    ))

    raw_archived = list(archived_customers_collection.find(
        {'agent_id': {'$in': accessible_agent_ids}},
        {'name': 1, 'phone_number': 1, 'purchases': 1, 'archived_at': 1, 'image_url': 1}
    ))

    customers = []
    for c in raw_customers:
        active_loans = list(db.loans.find(
            {'customer_id': c['_id'], 'status': {'$in': ['active','approved','grace_period','overdue']}},
        ).sort('created_at', -1))
        if active_loans:
            from routes.loans import sync_loan
            active_loans = [sync_loan(loan) for loan in active_loans]
            c['active_loans'] = [{
                'id': str(loan['_id']),
                'number': loan.get('loan_number'),
                'daily': float(money(loan.get('daily_repayment'))),
                'paid': float(money(loan.get('amount_paid'))),
                'balance': float(money(loan.get('current_balance'))),
                'penalties': float(money(loan.get('total_penalties'))),
                'status': loan.get('status'),
            } for loan in active_loans]
        c['_id'] = str(c['_id'])
        c['archived'] = False
        customers.append(_json_safe(c))
    for c in raw_archived:
        c['_id'] = str(c['_id'])
        c['archived'] = True
        customers.append(_json_safe(c))

    return render_template('add_payment.html', customers=customers)


@payment_bp.route('/all_payments', methods=['GET'])
@login_required
def view_all_payments():
    selected_date = request.args.get('date') or datetime.today().strftime('%Y-%m-%d')
    accessible_agent_ids = get_accessible_agent_ids(current_user.id)

    agent_customers = list(customers_collection.find({'agent_id': {'$in': accessible_agent_ids}}))
    agent_customer_ids = [c['_id'] for c in agent_customers]

    payments_on_date = list(payments_collection.find({
        'customer_id': {'$in': agent_customer_ids},
        'date': selected_date,
        'payment_type': {'$ne': 'WITHDRAWAL'}  # ✅ Exclude withdrawals
    }))

    grouped = {}
    for p in payments_on_date:
        customer = customers_collection.find_one({'_id': p.get('customer_id')})
        if not customer:
            continue

        cid = str(p['customer_id'])
        if cid not in grouped:
            grouped[cid] = {
                'customer_name': customer.get('name', 'Unknown'),
                'phone_number': customer.get('phone_number', 'N/A'),
                'date': selected_date,
                'amounts': [],
                'total_amount': 0.0,
                'payment_count': 0
            }

        grouped[cid]['amounts'].append(f"{float(p.get('amount', 0)):.2f}")
        grouped[cid]['total_amount'] += float(p.get('amount', 0))
        grouped[cid]['payment_count'] += 1

    summaries = list(grouped.values())
    return render_template('view_all_payments.html', payments=summaries, selected_date=selected_date)


@payment_bp.route('/payment/product_paid', methods=['GET'])
def get_product_paid():
    if not current_user.is_authenticated:
        return jsonify(ok=False, message='Unauthorized.'), 401
    customer_id = request.args.get('customer_id')
    product_index_raw = request.args.get('product_index')
    if not customer_id or product_index_raw is None:
        return jsonify(ok=False, message='Missing customer or product.'), 400

    try:
        cust_oid = ObjectId(customer_id)
        product_index = int(product_index_raw)
    except Exception:
        return jsonify(ok=False, message='Invalid customer or product.'), 400

    customer = customers_collection.find_one({
        '_id': cust_oid,
        'agent_id': {'$in': get_accessible_agent_ids(current_user.id)}
    })
    if not customer:
        return jsonify(ok=False, message='Unauthorized or customer not found.'), 403

    purchases = customer.get('purchases', [])
    if product_index < 0 or product_index >= len(purchases):
        return jsonify(ok=False, message='Product not found for this customer.'), 404

    product = purchases[product_index].get('product', {})
    try:
        product_total = float(product.get('total', 0))
    except Exception:
        product_total = 0.0

    deposits = payments_collection.find({
        'customer_id': cust_oid,
        'payment_type': 'PRODUCT',
        'product_index': product_index
    })
    withdrawals = payments_collection.find({
        'customer_id': cust_oid,
        'payment_type': 'WITHDRAWAL',
        'product_index': product_index
    })

    paid_sum = sum(float(p.get('amount', 0)) for p in deposits) - sum(float(p.get('amount', 0)) for p in withdrawals)
    paid_sum = round(paid_sum, 2)
    amount_left = max(0, round(product_total - paid_sum, 2))

    return jsonify(ok=True, paid=paid_sum, total=product_total, left=amount_left)
