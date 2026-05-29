from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select

from .config import DATA_DIR, DB_PATH, DEFAULT_EXPIRY_MONTHS
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

    growth_gb_per_day: float | None
    days_to_full: float | None
    days_to_expiry: int | None

    has_issue: bool
    severity: str  # ok|warn|bad


def _days_to_expiry(expire_date: date | None, reference: date | None) -> int | None:
    if not expire_date or not reference:
        return None
    return (expire_date - reference).days


def _safe_div(n: float | None, d: float | None) -> float | None:
    if n is None or d is None or d == 0:
        return None
    return n / d


def _num_for_sort(value: float | int | None, *, reverse: bool) -> float:
    if value is None:
        return float("-inf") if reverse else float("inf")
    return float(value)


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    sort: str = "severity",
    direction: str = "desc",
    show: str = "issues",
):
    with SessionLocal() as session:
        latest = _latest_report_date(session)
        counts = {
            "tenants": session.execute(select(func.count(Tenant.customer_id))).scalar_one(),
            "uploads": session.execute(select(func.count(ReportUpload.id))).scalar_one(),
            "latest_report_date": latest,
        }

        top = []
        if latest:
            top = session.execute(
                select(Tenant, TenantUsage)
                .join(TenantUsage, TenantUsage.customer_id == Tenant.customer_id)
                .where(TenantUsage.report_date == latest)
                .order_by(desc(TenantUsage.storage_usage_pct))
                .limit(20)
            ).all()

        cards: list[TenantCard] = []
        if latest:
            latest_usage_sq = (
                select(
                    TenantUsage.customer_id.label("customer_id"),
                    TenantUsage.used_storage_gb.label("used_gb"),
                    TenantUsage.storage_capacity_gb.label("capacity_gb"),
                    TenantUsage.storage_usage_pct.label("usage_pct"),
                    TenantUsage.exceeded_storage_gb.label("exceeded_gb"),
                )
                .where(TenantUsage.report_date == latest)
                .subquery()
            )

            prev_report_date_sq = (
                select(func.max(TenantUsage.report_date))
                .where(TenantUsage.customer_id == Tenant.customer_id, TenantUsage.report_date < latest)
                .correlate(Tenant)
                .scalar_subquery()
            )

            prev_used_sq = (
                select(TenantUsage.used_storage_gb)
                .where(TenantUsage.customer_id == Tenant.customer_id, TenantUsage.report_date == prev_report_date_sq)
                .correlate(Tenant)
                .scalar_subquery()
            )

            prev_date_sq = prev_report_date_sq.label("prev_report_date")

            rows = session.execute(
                select(
                    Tenant,
                    latest_usage_sq.c.used_gb,
                    latest_usage_sq.c.capacity_gb,
                    latest_usage_sq.c.usage_pct,
                    latest_usage_sq.c.exceeded_gb,
                    prev_used_sq.label("prev_used_gb"),
                    prev_date_sq,
                )
                .join(latest_usage_sq, latest_usage_sq.c.customer_id == Tenant.customer_id)
            ).all()

            for tenant, used_gb, capacity_gb, usage_pct, exceeded_gb, prev_used_gb, prev_report_date in rows:
                growth = None
                if used_gb is not None and prev_used_gb is not None and prev_report_date:
                    day_span = (latest - prev_report_date).days
                    growth = _safe_div(used_gb - prev_used_gb, float(day_span)) if day_span > 0 else None

                days_to_full = None
                if growth is not None and growth > 0 and used_gb is not None and capacity_gb is not None:
                    remaining = capacity_gb - used_gb
                    days_to_full = 0.0 if remaining <= 0 else remaining / growth

                days_to_expiry = _days_to_expiry(tenant.expire_date, latest)

                is_expired = (days_to_expiry is not None) and (days_to_expiry < 0)
                is_expiring_soon = (days_to_expiry is not None) and (0 <= days_to_expiry <= 90)
                is_over = (exceeded_gb or 0) > 0
                is_near_full = (usage_pct or 0) >= 90
                is_days_to_full_soon = (days_to_full is not None) and (days_to_full <= 60)

                has_issue = is_over or is_near_full or is_days_to_full_soon or is_expired or is_expiring_soon
                severity = "ok"
                if is_expired or is_over or ((usage_pct or 0) >= 98):
                    severity = "bad"
                elif has_issue:
                    severity = "warn"

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
                        growth_gb_per_day=growth,
                        days_to_full=days_to_full,
                        days_to_expiry=days_to_expiry,
                        has_issue=has_issue,
                        severity=severity,
                    )
                )

        if show == "issues":
            cards = [c for c in cards if c.has_issue]

        direction = (direction or "desc").lower()
        reverse = direction != "asc"

        def sort_fn(c: TenantCard):
            if sort == "growth":
                return _num_for_sort(c.growth_gb_per_day, reverse=reverse)
            if sort == "days_to_full":
                # smaller is worse, so default direction for this is asc
                return _num_for_sort(c.days_to_full, reverse=reverse)
            if sort == "days_to_expiry":
                return _num_for_sort(c.days_to_expiry, reverse=reverse)
            if sort == "usage_pct":
                return _num_for_sort(c.usage_pct, reverse=reverse)
            if sort == "exceeded":
                return _num_for_sort(c.exceeded_gb, reverse=reverse)
            if sort == "tenant":
                return (0, (c.tenant_name or "").lower())
            if sort == "severity":
                sev = {"ok": 0, "warn": 1, "bad": 2}.get(c.severity, 0)
                return sev
            return _num_for_sort(c.usage_pct, reverse=reverse)

        cards.sort(key=sort_fn, reverse=reverse)

        return templates.TemplateResponse(
            "home.html",
            {
                "request": request,
                "counts": counts,
                "top": top,
                "cards": cards,
                "sort": sort,
                "direction": direction,
                "show": show,
            },
        )


@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    return templates.TemplateResponse("upload.html", {"request": request})


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    content = await file.read()
    with SessionLocal() as session:
        ingest_report(session, file.filename, content)
    return RedirectResponse(url="/", status_code=303)


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
                .limit(12)
            )
            .scalars()
            .all()
        )
        hist = list(reversed(hist))

        forecast = None
        if hist:
            dates = [h.report_date for h in hist]
            used = [h.used_storage_gb for h in hist]
            cap = hist[-1].storage_capacity_gb if hist[-1].storage_capacity_gb is not None else None
            forecast = forecast_linear(dates, used, cap)

        return templates.TemplateResponse(
            "tenant_summary.html",
            {
                "request": request,
                "tenant": tenant,
                "latest_report_date": latest,
                "latest_usage": latest_usage,
                "history": hist,
                "forecast": forecast,
            },
        )


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
