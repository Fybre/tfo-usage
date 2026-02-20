import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
from dateutil import parser
from sqlalchemy import select

from .models import ReportUpload, Tenant, TenantUsage

COL_REPORT_DATE = "Report Date"
COL_CUSTOMER_ID = "Customer ID"
COL_TENANT_NAME = "Tenant Name"
COL_CUSTOMER_NAME = "Customer Name"
COL_RESELLER = "Reseller"
COL_EXPIRE_DATE = "Expire Date"
COL_CREATION_DATE = "Creation Date"

COL_USED_GB = "Used Storage\n(GB)"
COL_CAPACITY_GB = "Storage Capacity\n(GB)"
COL_USAGE_PCT = "Storage Usage\n(%)"
COL_EXCEEDED_GB = "Exceeded Storage\n(GB)"


def _file_hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _to_date(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        try:
            dt = parser.parse(str(value))
        except Exception:
            return None
    return dt.date()


def _to_datetime(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        try:
            dt = parser.parse(str(value))
        except Exception:
            return None
    return dt.to_pydatetime() if hasattr(dt, "to_pydatetime") else dt


def _to_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if pd.isna(value):
            return None
        return float(value)
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none"}:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def ingest_report(session, filename: str, content: bytes) -> ReportUpload:
    file_hash = _file_hash_bytes(content)
    existing = session.execute(select(ReportUpload).where(ReportUpload.file_hash == file_hash)).scalar_one_or_none()
    if existing:
        return existing

    tmp = Path("/tmp") / f"upload_{file_hash}.xlsx"
    tmp.write_bytes(content)
    df = pd.read_excel(tmp, engine="openpyxl")

    report_dt = pd.to_datetime(df[COL_REPORT_DATE].iloc[0], errors="coerce")
    if pd.isna(report_dt):
        report_dt = parser.parse(str(df[COL_REPORT_DATE].iloc[0]))
    report_date = report_dt.date()

    upload = ReportUpload(
        uploaded_at=datetime.utcnow(),
        source_filename=filename,
        file_hash=file_hash,
        report_date=report_date,
        row_count=int(len(df)),
    )
    session.add(upload)
    session.flush()

    for _, row in df.iterrows():
        customer_id = str(row.get(COL_CUSTOMER_ID, "") or "").strip()
        if not customer_id:
            continue

        tenant = session.get(Tenant, customer_id)
        if not tenant:
            tenant = Tenant(customer_id=customer_id)
            session.add(tenant)

        tenant.tenant_name = (str(row.get(COL_TENANT_NAME, "") or "").strip() or tenant.tenant_name)
        tenant.customer_name = (str(row.get(COL_CUSTOMER_NAME, "") or "").strip() or tenant.customer_name)
        tenant.reseller = (str(row.get(COL_RESELLER, "") or "").strip() or tenant.reseller)

        exp = _to_date(row.get(COL_EXPIRE_DATE))
        if exp:
            tenant.expire_date = exp

        created = _to_datetime(row.get(COL_CREATION_DATE))
        if created:
            tenant.creation_date = created

        usage = TenantUsage(
            report_upload_id=upload.id,
            report_date=report_date,
            customer_id=customer_id,
            used_storage_gb=_to_float(row.get(COL_USED_GB)),
            storage_capacity_gb=_to_float(row.get(COL_CAPACITY_GB)),
            storage_usage_pct=_to_float(row.get(COL_USAGE_PCT)),
            exceeded_storage_gb=_to_float(row.get(COL_EXCEEDED_GB)),
            raw_json=json.dumps({k: (None if (isinstance(v, float) and pd.isna(v)) else v) for k, v in row.to_dict().items()}, default=str),
        )
        session.add(usage)

    session.commit()
    return upload
