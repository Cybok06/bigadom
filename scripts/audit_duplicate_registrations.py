"""Read-only duplicate audit. Prints references/timing, never phone numbers or credentials."""
import ast
from collections import defaultdict
import json
import os
from pathlib import Path

from pymongo import MongoClient


def main():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'db.py').read_text())
    defaults = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in {'DEFAULT_URI', 'DEFAULT_DB_NAME'}}
    client = MongoClient(os.getenv('MONGODB_URI') or os.getenv('MONGO_URI') or defaults['DEFAULT_URI'],
                         serverSelectionTimeoutMS=15000, socketTimeoutMS=30000)
    db = client[os.getenv('MONGODB_DB') or os.getenv('MONGO_DB_NAME') or defaults['DEFAULT_DB_NAME']]
    try:
        loans = list(db.loans.find({}, {'loan_number': 1, 'customer_id': 1, 'original_amount': 1,
                     'created_at': 1, 'created_by': 1, 'status': 1, 'guarantor_details': 1, 'customer_extra_details': 1}))
        customers = list(db.customers.find({}, {'name': 1, 'phone_number': 1, 'agent_id': 1}))
        groups = defaultdict(list)
        for loan in loans:
            groups[(str(loan.get('customer_id')), str(loan.get('original_amount')))].append(loan)
        pairs = []
        for group in groups.values():
            group.sort(key=lambda x: str(x.get('created_at', '')))
            for a, b in zip(group, group[1:]):
                if a.get('created_at') and b.get('created_at'):
                    seconds = (b['created_at'] - a['created_at']).total_seconds()
                    if 0 <= seconds <= 300:
                        pairs.append({'references': [a.get('loan_number'), b.get('loan_number')],
                                      'seconds_apart': seconds, 'same_creator': a.get('created_by') == b.get('created_by'),
                                      'same_details': all(a.get(k) == b.get(k) for k in ('guarantor_details', 'customer_extra_details'))})
        identities = defaultdict(int)
        for c in customers:
            phone = ''.join(x for x in str(c.get('phone_number') or '') if x.isdigit())
            if phone:
                identities[(str(c.get('agent_id')), str(c.get('name') or '').strip().casefold(), phone)] += 1
        target = [x for x in loans if x.get('loan_number') in ('LNA-20260927-135C', 'LNA-20260927-D5D9')]
        for x in target:
            x.pop('guarantor_details', None)
            x.pop('customer_extra_details', None)
        print(json.dumps({'loan_count': len(loans), 'customer_count': len(customers),
                          'same_name_phone_owner_groups': sum(n > 1 for n in identities.values()),
                          'loans_within_five_minutes': pairs, 'screenshot_loans': target}, default=str, indent=2))
    except Exception as exc:
        print('Audit failed: ' + type(exc).__name__)
        raise SystemExit(1)
    finally:
        client.close()


if __name__ == '__main__':
    main()
