"""Move customer ownership and open loans together, preserving receipt history."""
from datetime import datetime, timezone
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

OPEN_LOAN_STATUSES = ["pending", "approved", "active", "grace_period", "overdue"]


def transfer_customers(database, match_filter, to_agent_id, manager_id, log_details):
    def save(mongo_session):
        customers = list(database.customers.find(
            match_filter,
            {"name": 1, "phone_number": 1, "location": 1, "occupation": 1},
            session=mongo_session,
        ))
        if not customers:
            return {"updated_customers": 0, "updated_loans": 0}
        customer_ids = [c["_id"] for c in customers]
        # Old records can use either strings or ObjectIds for customer_id.
        loan_customer_ids = list(dict.fromkeys(
            value for cid in customer_ids for value in (cid, str(cid))
        ))
        ownership = {"agent_id": str(to_agent_id)}
        if manager_id is not None:
            ownership["manager_id"] = manager_id
        customer_result = database.customers.update_many(
            {"$and": [match_filter, {"_id": {"$in": customer_ids}}]},
            {"$set": ownership}, session=mongo_session,
        )
        loan_result = database.loans.update_many(
            {"customer_id": {"$in": loan_customer_ids},
             "status": {"$in": OPEN_LOAN_STATUSES}},
            {"$set": ownership}, session=mongo_session,
        )
        counts = {"updated_customers": customer_result.modified_count,
                  "updated_loans": loan_result.modified_count}
        database.customer_transfer_logs.insert_one({
            **log_details, **counts, "total_customers": len(customers),
            "performed_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "customers": [{"id": str(c["_id"]), **{
                key: c.get(key, "") for key in
                ("name", "phone_number", "location", "occupation")
            }} for c in customers],
        }, session=mongo_session)
        return counts

    # No non-transactional fallback: a failure must leave both owners unchanged.
    with database.client.start_session() as mongo_session:
        return mongo_session.with_transaction(
            save, read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
        )
