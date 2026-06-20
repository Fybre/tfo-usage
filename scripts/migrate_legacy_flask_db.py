"""One-off migration from the old Flask app's SQLite db into this app's schema.

Usage:
    python -m scripts.migrate_legacy_flask_db <path-to-old-tfo_usage.db>

Reads the old `tenant` / `usage_record` tables and writes Tenant / TenantUsage
rows (plus one synthetic ReportUpload per distinct report date) into the
database configured via DB_PATH.
"""

import sqlite3
import sys
from datetime import datetime

from app.config import DATA_DIR, DB_PATH
from app.db import make_session_factory, sqlite_engine
from app.models import Base, ReportUpload, Tenant, TenantUsage


def main(old_db_path: str) -> None:
    old = sqlite3.connect(old_db_path)
    old.row_factory = sqlite3.Row

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine = sqlite_engine(str(DB_PATH))
    SessionLocal = make_session_factory(engine)
    Base.metadata.create_all(bind=engine)

    tenants_by_old_id = {row["id"]: row for row in old.execute("SELECT * FROM tenant")}
    records = list(old.execute("SELECT * FROM usage_record ORDER BY report_date"))

    report_dates = sorted({r["report_date"][:10] for r in records})

    with SessionLocal() as session:
        upload_by_date = {}
        for d in report_dates:
            row_count = sum(1 for r in records if r["report_date"][:10] == d)
            upload = ReportUpload(
                uploaded_at=datetime.utcnow(),
                source_filename="migrated-from-legacy-flask-app",
                file_hash=f"legacy-migration-{d}",
                report_date=datetime.strptime(d, "%Y-%m-%d").date(),
                row_count=row_count,
            )
            session.add(upload)
            session.flush()
            upload_by_date[d] = upload.id

        tenant_count = 0
        for old_id, t in tenants_by_old_id.items():
            customer_id = t["customer_id"] or t["tenant_name"]
            tenant = session.get(Tenant, customer_id)
            if not tenant:
                tenant = Tenant(customer_id=customer_id)
                session.add(tenant)
            tenant.tenant_name = t["tenant_name"]
            tenant.customer_name = t["customer_name"]
            tenant_count += 1
        session.flush()

        usage_count = 0
        for r in records:
            t = tenants_by_old_id[r["tenant_id"]]
            customer_id = t["customer_id"] or t["tenant_name"]
            tenant = session.get(Tenant, customer_id)

            report_date_str = r["report_date"][:10]
            report_date = datetime.strptime(report_date_str, "%Y-%m-%d").date()

            if r["reseller_name"]:
                tenant.reseller = r["reseller_name"]
            if r["expire_date"]:
                tenant.expire_date = datetime.strptime(r["expire_date"][:10], "%Y-%m-%d").date()
            if r["creation_date"]:
                tenant.creation_date = datetime.strptime(r["creation_date"][:19], "%Y-%m-%d %H:%M:%S")

            existing = session.execute(
                __import__("sqlalchemy").select(TenantUsage).where(
                    TenantUsage.customer_id == customer_id,
                    TenantUsage.report_date == report_date,
                )
            ).scalar_one_or_none()
            if existing:
                continue

            usage = TenantUsage(
                report_upload_id=upload_by_date[report_date_str],
                report_date=report_date,
                customer_id=customer_id,
                used_storage_gb=r["used_storage_gb"],
                storage_capacity_gb=r["storage_capacity_gb"],
                storage_usage_pct=r["storage_usage_pct"],
                exceeded_storage_gb=r["exceeded_storage_gb"],
                concurrent_users=r["concurrent_user"],
                named_users=r["named_user"],
                read_only_users=r["read_only_user"],
                document_count=r["documents"],
            )
            session.add(usage)
            usage_count += 1

        session.commit()

    print(f"Migrated {tenant_count} tenants, {usage_count} usage records, {len(report_dates)} report dates.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.migrate_legacy_flask_db <path-to-old-tfo_usage.db>")
        sys.exit(1)
    main(sys.argv[1])
