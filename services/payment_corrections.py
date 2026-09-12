"""Transactional payment corrections; never rewrite original payment ownership or dates."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from bson import ObjectId
from sales_close_types import BALANCE_FIELDS
from services.change_activities import record_change


def amount_value(value):
    try:
        amount = value.to_decimal() if hasattr(value, 'to_decimal') else Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal('999999999.99') or amount != amount.quantize(Decimal('.01')):
            raise ValueError('Enter a positive amount with at most two decimal places.')
        return amount
    except (InvalidOperation, TypeError):
        raise ValueError('Enter a valid payment amount.')


def decimal_value(value):
    if hasattr(value, 'to_decimal'):
        return value.to_decimal()
    return Decimal(str(value or 0))


def id_values(value):
    values = [value, str(value)]
    if ObjectId.is_valid(str(value)):
        values.append(ObjectId(str(value)))
    return list(dict.fromkeys(values))


def correct_payment(database, payment_id, actor, new_amount, expected_amount):
    new_amount = amount_value(new_amount)
    expected_amount = amount_value(expected_amount)
    now = datetime.utcnow()

    def save(mongo_session):
        payment = database.payments.find_one({'_id': {'$in': id_values(payment_id)}}, session=mongo_session)
        if not payment:
            raise LookupError('Payment not found.')
        customer_query = {'_id': {'$in': id_values(payment.get('customer_id'))}}
        if actor['role'] == 'manager':
            customer_query['manager_id'] = {'$in': id_values(actor['user_id'])}
        customer = database.customers.find_one(customer_query, session=mongo_session)
        if not customer:
            raise LookupError('Customer not found or outside your access.')
        kind = payment.get('payment_type') or 'PRODUCT'
        if kind not in BALANCE_FIELDS:
            raise ValueError('Only product, SUSU and loan receipts can be corrected here.')
        old = amount_value(payment.get('amount'))
        if old != expected_amount:
            raise ValueError('Payment changed since you opened it. Reload and try again.')
        delta = new_amount - old
        if not delta:
            return False
        agent_id = payment.get('agent_id')
        if not agent_id or not payment.get('date'):
            raise ValueError('This payment is missing its original agent or ledger date. Reconcile it before editing.')
        field = BALANCE_FIELDS[kind]
        ledger = database.sales_close
        rows = list(ledger.find({'agent_id': {'$in': id_values(agent_id)}}, session=mongo_session))
        rows.sort(key=lambda row: (row.get('date') == payment['date'], str(row.get('date') or '')), reverse=True)
        adjustments = []
        if delta < 0:
            remaining = -delta
            # Use this receipt category, then unclassified historical balance.
            for row in rows:
                total = decimal_value(row.get('total_amount'))
                typed = decimal_value(row.get(field))
                legacy = max(Decimal(0), total - sum(decimal_value(row.get(f)) for f in BALANCE_FIELDS.values()))
                take_typed = min(max(Decimal(0), typed), remaining, max(Decimal(0), total))
                take_legacy = min(legacy, remaining - take_typed, max(Decimal(0), total - take_typed))
                take = take_typed + take_legacy
                if not take:
                    continue
                inc = {'total_amount': -float(take)}
                if take_typed:
                    inc[field] = -float(take_typed)
                ledger.update_one({'_id': row['_id']}, {'$inc': inc, '$set': {'updated_at': now}}, session=mongo_session)
                adjustments.append({'ledger_id': str(row['_id']), 'amount': -float(take), 'date': str(row.get('date'))})
                remaining -= take
                if not remaining:
                    break
            if remaining:
                raise ValueError('Insufficient unclosed money in this payment category. Reconcile money already closed or transferred before reducing this payment.')
        else:
            source = next((row for row in rows if row.get('date') == payment['date']), None)
            query = {'_id': source['_id']} if source else {'agent_id': str(agent_id), 'date': payment['date']}
            ledger.update_one(query, {'$inc': {'total_amount': float(delta), field: float(delta)},
                '$set': {'updated_at': now}, '$setOnInsert': {'agent_id': str(agent_id), 'date': payment['date'],
                'manager_id': payment.get('manager_id'), 'created_at': now}}, upsert=True, session=mongo_session)
            adjustments.append({'amount': float(delta), 'date': str(payment['date'])})
        if kind == 'LOAN':
            from services.loans import mongo_money, status_for
            loan = database.loans.find_one({'_id': {'$in': id_values(payment.get('loan_id'))}}, session=mongo_session)
            if not loan or str(loan.get('customer_id')) != str(customer['_id']) or loan.get('status') in {'cancelled', 'pending', 'rejected'}:
                raise ValueError('This loan cannot receive payment corrections in its current state.')
            paid = decimal_value(loan.get('amount_paid')) + delta
            balance = decimal_value(loan.get('current_balance')) - delta
            if paid < 0 or balance < 0:
                raise ValueError('The corrected payment would exceed the loan balance.')
            revised = {**loan, 'current_balance': balance}
            status = status_for(revised)
            update = {'$set': {'amount_paid': mongo_money(paid), 'current_balance': mongo_money(balance), 'status': status, 'updated_at': now}}
            if balance == 0:
                update['$set']['settled_at'] = now
            else:
                update['$unset'] = {'settled_at': ''}
            database.loans.update_one({'_id': loan['_id']}, update, session=mongo_session)
        database.payments.update_one({'_id': payment['_id']}, {'$set': {'amount': float(new_amount),
            'updated_at': now, 'updated_by': str(actor['user_id'])}}, session=mongo_session)
        if kind == 'PRODUCT' and delta < 0 and customer.get('status') == 'completed':
            database.customers.update_one({'_id': customer['_id']}, {'$set': {'status': 'payment_ongoing'}}, session=mongo_session)
        record_change(database, actor, 'payment.changed', customer,
            {'amount': {'from': float(old), 'to': float(new_amount)}}, mongo_session=mongo_session,
            entity_id=payment['_id'], extra={'agent_id': str(agent_id), 'payment_type': kind,
                'payment_date': str(payment['date']), 'unclosed_adjustment': float(delta), 'ledger_adjustments': adjustments})
        return True

    if actor.get('role') not in {'manager', 'executive'} or not actor.get('is_authenticated'):
        raise PermissionError('Only managers and executives may edit payments.')
    with database.client.start_session() as mongo_session:
        return mongo_session.with_transaction(save)
