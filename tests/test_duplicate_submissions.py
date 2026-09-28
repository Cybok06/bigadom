import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from bson import ObjectId
from flask import Flask
from pymongo.errors import DuplicateKeyError

from services.submission_tokens import new_submission_token, submission_id, insert_submission


class AtomicCollection:
    def __init__(self):
        self.docs = {}
        self.lock = Lock()

    def insert_one(self, doc):
        with self.lock:
            if doc['_id'] in self.docs:
                raise DuplicateKeyError('duplicate _id')
            self.docs[doc['_id']] = dict(doc)

    def find_one(self, query):
        return self.docs.get(query['_id'])


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only'
        self.app.config['LOGIN_DISABLED'] = True
        self.user = str(ObjectId())

    def test_tokens_are_bound_to_user_and_form(self):
        with self.app.app_context():
            token = new_submission_token('loan', self.user)
            self.assertEqual(submission_id(token, 'loan', self.user), submission_id(token, 'loan', self.user))
            for value, kind, user in [(token, 'customer', self.user), (token, 'loan', 'other'),
                                      ('', 'loan', self.user), (token + 'x', 'loan', self.user)]:
                with self.assertRaises(ValueError):
                    submission_id(value, kind, user)

    def test_concurrent_requests_create_one_record(self):
        collection = AtomicCollection()
        record_id = ObjectId()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: insert_submission(collection, {'name': 'Test'}, record_id), range(20)))
        self.assertEqual(len(collection.docs), 1)
        self.assertEqual(sum(created for _, created in results), 1)

    def test_other_unique_constraint_errors_are_not_hidden(self):
        collection = MagicMock()
        collection.insert_one.side_effect = DuplicateKeyError('loan number')
        collection.find_one.return_value = None
        with self.assertRaises(DuplicateKeyError):
            insert_submission(collection, {}, ObjectId())

    def load_route(self, filename):
        spec = importlib.util.spec_from_file_location('submission_route_test', Path(__file__).parents[1] / filename)
        module = importlib.util.module_from_spec(spec)
        activation = SimpleNamespace(get_accessible_agent_ids=lambda _: [self.user],
                                     get_activation_group_context=lambda *args: {},
                                     get_next_approved_activation_for_user=lambda *args: None)
        with patch.dict('sys.modules', {'db': SimpleNamespace(db=MagicMock()), 'services.activation_groups': activation}):
            spec.loader.exec_module(module)
        module.current_user = SimpleNamespace(id=self.user, role='agent', is_authenticated=True)
        return module

    def test_repeated_loan_post_and_new_form(self):
        module = self.load_route('routes/loans.py')
        self.app.register_blueprint(module.loans_bp)
        collection = AtomicCollection()
        module.loans_col = collection
        customer = {'_id': ObjectId(), 'agent_id': self.user, 'manager_id': ObjectId()}
        module._owned_customer = lambda _: customer
        with self.app.app_context():
            token = new_submission_token('loan', self.user)
        data = {'submission_token': token, 'customer_id': str(customer['_id']), 'amount': '1000'}
        client = self.app.test_client()
        for _ in range(2):
            self.assertEqual(client.post('/loans/apply', data=data).status_code, 302)
        self.assertEqual(len(collection.docs), 1)
        with self.app.app_context():
            data['submission_token'] = new_submission_token('loan', self.user)
        self.assertEqual(client.post('/loans/apply', data=data).status_code, 302)
        self.assertEqual(len(collection.docs), 2)
        data.pop('submission_token')
        client.post('/loans/apply', data=data)
        self.assertEqual(len(collection.docs), 2)

    def test_repeated_customer_post_returns_original_record(self):
        module = self.load_route('customer.py')
        self.app.register_blueprint(module.customer_bp, url_prefix='/customer')
        collection = AtomicCollection()
        module.customers_collection = collection
        module.users_collection.find_one.return_value = {'manager_id': ObjectId()}
        with self.app.app_context():
            token = new_submission_token('customer', self.user)
        data = {'submission_token': token, 'name': 'Test', 'phone_number': '0200000000'}
        client = self.app.test_client()
        first = client.post('/customer/add', data=data)
        second = client.post('/customer/add', data=data)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json['customer_id'], second.json['customer_id'])
        self.assertEqual(len(collection.docs), 1)
        data.pop('submission_token')
        self.assertEqual(client.post('/customer/add', data=data).status_code, 400)
        self.assertEqual(len(collection.docs), 1)


if __name__ == '__main__':
    unittest.main()
