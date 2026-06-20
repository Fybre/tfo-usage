from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from .config import DATA_DIR, DB_PATH, DEFAULT_EXPIRY_MONTHS, IMMINENT_DAYS
from .db import make_session_factory, sqlite_engine
from .forecast import forecast_linear
from .ingest import ingest_report
from .models import Base, ReportUpload, Tenant, TenantUsage

app = FastAPI(title="TFO Usage (internal)")

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
engine = sqlite_engine(str(DB_PATH))
SessionLocal = make_session_factory(engine)
Base.metadata.create_all(bind=engine)


def _latest_report_date(session) -> date | None:
    return session.execute(select(func.max(ReportUpload.report_date))).scalar_one()


@dataclass(frozen=True)
class TenantCard:
    customer_id: str
    tenant_name: str | None
    customer_name: str | None
    reseller: str | None
    expire_date: date | None

    used_gb: float | None
    capacity_gb: float | None
    usage_pct: float | None
    exceeded_gb: float | None

    concurrent_users: int | None
    named_users: int | None
    read_only_users: int | None
    document_count: int | None

    growth_gb_per_day: float | None
    growth_gb_per_month: float | None
    days_to_full: float | None
    est_full_date: date | None
    days_to_expiry: int | None

    over_capacity: bool
    eta_within_threshold: bool
    expired: bool
    expiry_within_threshold: bool


def _days_to_expiry(expire_date: date | None, reference: date | None) -> int | None:
    if not expire_date or not reference:
        return None
    return (expire_date - reference).days



@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str = "", submitted: str = "", hide_expired: str = ""):
    hide_expired_checked = (hide_expired == "1") if submitted == "1" else True
    with SessionLocal() as session:
        latest = _latest_report_date(session)
        counts = {
            "tenants": session.execute(select(func.count(Tenant.customer_id))).scalar_one(),
            "uploads": session.execute(select(func.count(ReportUpload.id))).scalar_one(),
            "latest_report_date": latest,
        }

        cards: list[TenantCard] = []
        if latest:
            tenants = session.execute(select(Tenant)).scalars().all()

            usage_rows = session.execute(
                select(TenantUsage).order_by(TenantUsage.customer_id, TenantUsage.report_date)
            ).scalars().all()

            history_by_tenant: dict[str, list[TenantUsage]] = {}
            for u in usage_rows:
                history_by_tenant.setdefault(u.customer_id, []).append(u)

            for tenant in tenants:
                hist = history_by_tenant.get(tenant.customer_id)
                if not hist or hist[-1].report_date != latest:
                    continue

                latest_usage = hist[-1]
                used_gb = latest_usage.used_storage_gb
                capacity_gb = latest_usage.storage_capacity_gb
                exceeded_gb = latest_usage.exceeded_storage_gb
                concurrent_users = latest_usage.concurrent_users
                named_users = latest_usage.named_users
                read_only_users = latest_usage.read_only_users
                document_count = latest_usage.document_count

                usage_pct = (used_gb or 0) / capacity_gb * 100.0 if capacity_gb else (latest_usage.storage_usage_pct or 0)

                dates = [h.report_date for h in hist]
                used_series = [h.used_storage_gb for h in hist]
                forecast = forecast_linear(dates, used_series, capacity_gb)
                growth = forecast.growth_gb_per_day
                days_to_full = forecast.days_to_full

                est_full_date = None
                if days_to_full is not None and days_to_full <= 50 * 365:
                    est_full_date = latest + timedelta(days=days_to_full)

                days_to_expiry = _days_to_expiry(tenant.expire_date, latest)

                expired = (days_to_expiry is not None) and (days_to_expiry < 0)
                expiry_within_threshold = (days_to_expiry is not None) and (0 <= days_to_expiry <= IMMINENT_DAYS)
                over_capacity = (
                    (capacity_gb and used_gb is not None and used_gb > capacity_gb)
                    or ((usage_pct or 0) > 100.0)
                    or ((exceeded_gb or 0) > 0)
                )
                eta_within_threshold = (days_to_full is not None) and (days_to_full <= IMMINENT_DAYS)

                if q and q.lower() not in (tenant.tenant_name or "").lower() and q.lower() not in (
                    tenant.customer_name or ""
                ).lower():
                    continue
                if hide_expired_checked and expired:
                    continue

                cards.append(
                    TenantCard(
                        customer_id=tenant.customer_id,
                        tenant_name=tenant.tenant_name,
                        customer_name=tenant.customer_name,
                        reseller=tenant.reseller,
                        expire_date=tenant.expire_date,
                        used_gb=used_gb,
                        capacity_gb=capacity_gb,
                        usage_pct=usage_pct,
                        exceeded_gb=exceeded_gb,
                        concurrent_users=concurrent_users,
                        named_users=named_users,
                        read_only_users=read_only_users,
                        document_count=document_count,
                        growth_gb_per_day=growth,
                        growth_gb_per_month=(growth * 30 if growth is not None else None),
                        days_to_full=days_to_full,
                        est_full_date=est_full_date,
                        days_to_expiry=days_to_expiry,
                        over_capacity=over_capacity,
                        eta_within_threshold=eta_within_threshold,
                        expired=expired,
                        expiry_within_threshold=expiry_within_threshold,
                    )
                )

        def sort_key(c: TenantCard):
            critical = c.over_capacity or c.expired
            warning = (not critical) and (c.eta_within_threshold or c.expiry_within_threshold)
            return (not critical, not warning, (c.customer_name or c.tenant_name or "").lower())

        cards.sort(key=sort_key)

        flagged_over = sum(1 for c in cards if c.over_capacity)
        flagged_eta = sum(1 for c in cards if c.eta_within_threshold)
        flagged_expired = sum(1 for c in cards if c.expired)
        flagged_expiry = sum(1 for c in cards if c.expiry_within_threshold)

        return templates.TemplateResponse(
            "home.html",
            {
                "request": request,
                "counts": counts,
                "cards": cards,
                "q": q,
                "hide_expired": hide_expired_checked,
                "imminent_days": IMMINENT_DAYS,
                "flagged_over": flagged_over,
                "flagged_eta": flagged_eta,
                "flagged_expired": flagged_expired,
                "flagged_expiry": flagged_expiry,
            },
        )


@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    return templates.TemplateResponse("upload.html", {"request": request, "results": None})


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, files: list[UploadFile] = File(...)):
    results = []
    with SessionLocal() as session:
        for file in files:
            content = await file.read()
            try:
                upload, created = ingest_report(session, file.filename, content)
                results.append(
                    {
                        "filename": file.filename,
                        "status": "imported" if created else "duplicate",
                        "report_date": upload.report_date,
                        "row_count": upload.row_count,
                    }
                )
            except Exception as exc:
                session.rollback()
                results.append({"filename": file.filename, "status": "error", "error": str(exc)})

    if len(files) == 1 and results and results[0]["status"] != "error":
        return RedirectResponse(url="/", status_code=303)

    return templates.TemplateResponse("upload.html", {"request": request, "results": results})


@app.get("/tenants", response_class=HTMLResponse)
def tenants_search_page(request: Request, q: str | None = None):
    with SessionLocal() as session:
        results = []
        if q:
            q_strip = q.strip()
            if session.get(Tenant, q_strip):
                return RedirectResponse(url=f"/tenants/{q_strip}", status_code=303)

            like = f"%{q_strip}%"
            results = (
                session.execute(
                    select(Tenant)
                    .where(
                        (Tenant.tenant_name.ilike(like))
                        | (Tenant.customer_name.ilike(like))
                        | (Tenant.customer_id.ilike(like))
                    )
                    .order_by(Tenant.tenant_name)
                    .limit(100)
                )
                .scalars()
                .all()
            )

        return templates.TemplateResponse("tenants_search.html", {"request": request, "q": q or "", "results": results})


@app.get("/tenants/expiry", response_class=HTMLResponse)
def tenants_expiry(request: Request, months: int = DEFAULT_EXPIRY_MONTHS, reference: str = "today"):
    with SessionLocal() as session:
        ref = date.today()
        if reference == "latest_report":
            ref = _latest_report_date(session) or ref

        cutoff = ref + relativedelta(months=months)

        expired = (
            session.execute(
                select(Tenant)
                .where(Tenant.expire_date.is_not(None), Tenant.expire_date < ref)
                .order_by(Tenant.expire_date.asc())
            )
            .scalars()
            .all()
        )

        expiring = (
            session.execute(
                select(Tenant)
                .where(Tenant.expire_date.is_not(None), Tenant.expire_date >= ref, Tenant.expire_date < cutoff)
                .order_by(Tenant.expire_date.asc())
            )
            .scalars()
            .all()
        )

        return templates.TemplateResponse(
            "tenants_expiry.html",
            {
                "request": request,
                "months": months,
                "reference": reference,
                "ref": ref,
                "cutoff": cutoff,
                "expired": expired,
                "expiring": expiring,
            },
        )


@app.get("/tenants/{customer_id}", response_class=HTMLResponse)
def tenant_summary(request: Request, customer_id: str):
    with SessionLocal() as session:
        tenant = session.get(Tenant, customer_id)
        if not tenant:
            return templates.TemplateResponse(
                "not_found.html",
                {"request": request, "message": "Tenant not found"},
                status_code=404,
            )

        latest = _latest_report_date(session)
        latest_usage = None
        if latest:
            latest_usage = session.execute(
                select(TenantUsage).where(TenantUsage.customer_id == customer_id, TenantUsage.report_date == latest)
            ).scalar_one_or_none()

        hist = (
            session.execute(
                select(TenantUsage)
                .where(TenantUsage.customer_id == customer_id)
                .order_by(TenantUsage.report_date.desc())
            )
            .scalars()
            .all()
        )
        hist = list(reversed(hist))

        forecast = None
        days_to_expiry = None
        over_capacity = False
        eta_within_threshold = False
        expired = False
        expiry_within_threshold = False
        est_full_date = None
        latest_usage_pct = None

        if hist:
            dates = [h.report_date for h in hist]
            used = [h.used_storage_gb for h in hist]
            cap = hist[-1].storage_capacity_gb if hist[-1].storage_capacity_gb is not None else None
            forecast = forecast_linear(dates, used, cap)

            days_to_expiry = _days_to_expiry(tenant.expire_date, latest_usage.report_date if latest_usage else None)
            expired = (days_to_expiry is not None) and (days_to_expiry < 0)
            expiry_within_threshold = (days_to_expiry is not None) and (0 <= days_to_expiry <= IMMINENT_DAYS)

            if latest_usage:
                used_gb = latest_usage.used_storage_gb
                capacity_gb = latest_usage.storage_capacity_gb
                latest_usage_pct = (used_gb or 0) / capacity_gb * 100.0 if capacity_gb else (latest_usage.storage_usage_pct or 0)
                over_capacity = (
                    (capacity_gb and used_gb is not None and used_gb > capacity_gb)
                    or (latest_usage_pct > 100.0)
                    or ((latest_usage.exceeded_storage_gb or 0) > 0)
                )

            if forecast.days_to_full is not None:
                eta_within_threshold = forecast.days_to_full <= IMMINENT_DAYS
                if forecast.days_to_full <= 50 * 365 and latest_usage:
                    est_full_date = latest_usage.report_date + timedelta(days=forecast.days_to_full)

        return templates.TemplateResponse(
            "tenant_summary.html",
            {
                "request": request,
                "tenant": tenant,
                "latest_report_date": latest,
                "latest_usage": latest_usage,
                "latest_usage_pct": latest_usage_pct,
                "history": hist,
                "forecast": forecast,
                "imminent_days": IMMINENT_DAYS,
                "days_to_expiry": days_to_expiry,
                "over_capacity": over_capacity,
                "eta_within_threshold": eta_within_threshold,
                "expired": expired,
                "expiry_within_threshold": expiry_within_threshold,
                "est_full_date": est_full_date,
                "chart_labels": [h.report_date.isoformat() for h in hist],
                "chart_used": [h.used_storage_gb or 0 for h in hist],
                "chart_capacity": [h.storage_capacity_gb or 0 for h in hist],
            },
        )


@app.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request):
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return templates.TemplateResponse(
        "backup.html",
        {"request": request, "db_size": db_size},
    )


@app.get("/backup/download")
def backup_download():
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database file not found")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return FileResponse(
        DB_PATH,
        media_type="application/octet-stream",
        filename=f"tfo-usage-backup-{stamp}.db",
    )


@app.post("/backup/restore")
async def backup_restore(file: UploadFile = File(...)):
    content = await file.read()
    if content[:16] != b"SQLite format 3\x00":
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid SQLite database")

    engine.dispose()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    pre_restore_backup = DATA_DIR / f"pre-restore-{stamp}.db"
    if DB_PATH.exists():
        shutil.copy2(DB_PATH, pre_restore_backup)

    DB_PATH.write_bytes(content)

    return RedirectResponse(url="/backup", status_code=303)
