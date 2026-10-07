"""Signed, user-bound form IDs backed by MongoDB's atomic unique _id constraint."""
from bson import ObjectId
from flask import current_app
from itsdangerous import BadData, URLSafeSerializer
from pymongo.errors import DuplicateKeyError


def _signer(kind):
    return URLSafeSerializer(current_app.secret_key, salt='submission:' + kind)


def new_submission_token(kind, user_id):
    return _signer(kind).dumps({'user': str(user_id), 'id': str(ObjectId())})


def submission_id(token, kind, user_id):
    try:
        data = _signer(kind).loads(token or '')
        if data['user'] != str(user_id):
            raise ValueError()
        return ObjectId(data['id'])
    except (BadData, ValueError, TypeError, KeyError):
        raise ValueError('This form is invalid. Reload the page and try again.') from None


def insert_submission(collection, document, record_id):
    document['_id'] = record_id
    try:
        collection.insert_one(document)
        return document, True
    except DuplicateKeyError:
        # Do not swallow collisions on other unique indexes (e.g. loan_number).
        existing = collection.find_one({'_id': record_id})
        if existing is None:
            raise
        return existing, False
