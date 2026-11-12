"""Apply a reviewed BSON ownership-repair plan atomically; default is read-only."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bson import json_util
from pymongo import UpdateOne
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern


def apply_plan(database, plan, repair_id):
    def save(session):
        previous = database.customer_transfer_loan_repairs.find_one({"_id": repair_id}, session=session)
        if previous:
            return previous
        customers = {str(c["_id"]): c for c in database.customers.find(
            {"_id": {"$in": [p["customer_id"] for p in plan]}}, session=session)}
        loans = {l["_id"]: l for l in database.loans.find(
            {"_id": {"$in": [p["loan_before"]["_id"] for p in plan]}}, session=session)}
        updates = []
        for item in plan:
            customer = customers.get(str(item["customer_id"]), {})
            if (customer.get("agent_id") != item["expected_customer_agent_id"] or
                    customer.get("manager_id") != item["expected_customer_manager_id"]):
                raise RuntimeError("Customer ownership changed since the backup; rebuild the plan.")
            before = item["loan_before"]
            if loans.get(before["_id"]) != before:
                raise RuntimeError("Loan changed since the backup; rebuild the plan.")
            updates.append(UpdateOne({"_id": before["_id"]}, {"$set": item["new_ownership"]}))
        if not updates:
            raise RuntimeError("Empty repair plan")
        result = database.loans.bulk_write(updates, ordered=True, session=session)
        if result.matched_count != len(plan) or result.modified_count != len(plan):
            raise RuntimeError("Unexpected repair count; rolling back")
        after = {l["_id"]: l for l in database.loans.find(
            {"_id": {"$in": list(loans)}}, session=session)}
        for item in plan:
            expected = {**item["loan_before"], **item["new_ownership"]}
            if after.get(expected["_id"]) != expected:
                raise RuntimeError("A field other than ownership changed; rolling back")
        record = {"_id": repair_id, "applied_at": datetime.now(timezone.utc),
                  "loan_count": len(plan), "customer_count": len(customers),
                  "financial_fields_unchanged": True,
                  "changes": [{"loan_id": p["loan_before"]["_id"], "customer_id": p["customer_id"],
                               "old_agent_id": p["loan_before"].get("agent_id"),
                               "old_manager_id": p["loan_before"].get("manager_id"),
                               "new_ownership": p["new_ownership"],
                               "transfer_log_id": p["transfer_log_id"]} for p in plan]}
        database.customer_transfer_loan_repairs.insert_one(record, session=session)
        return record
    with database.client.start_session() as session:
        return session.with_transaction(save, read_concern=ReadConcern("snapshot"),
                                        write_concern=WriteConcern("majority"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    raw = args.plan.read_bytes()
    plan = json_util.loads(raw.decode("utf-8"))
    repair_id = "loan-transfer-ownership-" + hashlib.sha256(raw).hexdigest()
    if not args.apply:
        print(json.dumps({"planned_loans": len(plan), "repair_id": repair_id, "applied": False}))
    else:
        from db import db
        result = apply_plan(db, plan, repair_id)
        args.plan.with_name("repair-result.bson.json").write_text(json_util.dumps(result, indent=2), encoding="utf-8")
        print(json_util.dumps({k: v for k, v in result.items() if k != "changes"}, indent=2))
