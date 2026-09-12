"""Durable before/after records for customer-facing changes."""
from datetime import datetime


def record_change(database, actor, action, customer, changes, *, mongo_session, entity_id=None, extra=None):
    document = {
        'action': action, 'timestamp': datetime.utcnow(),
        'actor_id': str(actor.get('user_id') or ''),
        'actor_name': actor.get('name') or actor.get('user_id') or 'Unknown',
        'actor_role': actor.get('role') or '',
        'customer_id': str(customer.get('_id') or ''),
        'customer_name': customer.get('name') or '',
        'entity_id': str(entity_id or customer.get('_id') or ''),
        'changes': changes, 'extra': extra or {},
    }
    database.change_activities.insert_one(document, session=mongo_session)


def audited_details_update(database, collection, document, updates, actor, action, customer, *, history=None, metadata=None):
    changes = {key: {'from': document.get(key), 'to': value}
               for key, value in updates.items() if document.get(key) != value}
    if not changes:
        return False
    def save(mongo_session):
        # A stale form cannot silently replace a newer edit.
        query = {'_id': document['_id'], **{key: document.get(key) for key in updates}}
        result = collection.update_one(query, {'$set': {**updates, **(metadata or {}), 'updated_at': datetime.utcnow()}}, session=mongo_session)
        if result.matched_count != 1:
            raise ValueError('This record changed. Reload the page and try again.')
        record_change(database, actor, action, customer, changes, mongo_session=mongo_session, entity_id=document['_id'])
        if history:
            database.customer_change_history.insert_one(history, session=mongo_session)
        return True
    with database.client.start_session() as mongo_session:
        return mongo_session.with_transaction(save)
