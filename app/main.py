from datetime import date
from pathlib import Path

from dateutil.relativedelta import relativedelta
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc

from .config import DB_PATH, DATA_DIR, DEFAULT_EXPIRY_MONTHS
from .db import sqlite_engine, make_session_factory
from .models import Base, Tenant, TenantUsage, ReportUpload
from .ingest import ingest_report
from .forecast import forecast_linear

app = FastAPI(title="TFO Usage (internal)")

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
engine = sqlite_engine(str(DB_PATH))
SessionLocal = make_session_factory(engine)
Base.metadata.create_all(bind=engine)


def _latest_report_date(session):
    return session.execute(select(func.max(ReportUpload.report_date))).scalar_one()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
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

        return templates.TemplateResponse("home.html", {"request": request, "counts": counts, "top": top})


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
            results = session.execute(
                select(Tenant)
                .where((Tenant.tenant_name.ilike(like)) | (Tenant.customer_name.ilike(like)) | (Tenant.customer_id.ilike(like)))
                .order_by(Tenant.tenant_name)
                .limit(100)
            ).scalars().all()

        return templates.TemplateResponse("tenants_search.html", {"request": request, "q": q or "", "results": results})


@app.get("/tenants/{customer_id}", response_class=HTMLResponse)
def tenant_summary(request: Request, customer_id: str):
    with SessionLocal() as session:
        tenant = session.get(Tenant, customer_id)
        if not tenant:
            return templates.TemplateResponse("not_found.html", {"request": request, "message": "Tenant not found"}, status_code=404)

        latest = _latest_report_date(session)
        latest_usage = None
        if latest:
            latest_usage = session.execute(
                select(TenantUsage).where(TenantUsage.customer_id == customer_id, TenantUsage.report_date == latest)
            ).scalar_one_or_none()

        hist = session.execute(
            select(TenantUsage)
            .where(TenantUsage.customer_id == customer_id)
            .order_by(TenantUsage.report_date.desc())
            .limit(12)
        ).scalars().all()
        hist = list(reversed(hist))

        forecast = None
        if hist:
            dates = [h.report_date for h in hist]
            used = [h.used_storage_gb for h in hist]
            cap = hist[-1].storage_capacity_gb if hist[-1].storage_capacity_gb is not None else None
            forecast = forecast_linear(dates, used, cap)

        return templates.TemplateResponse(
            "tenant_summary.html",
            {"request": request, "tenant": tenant, "latest_report_date": latest, "latest_usage": latest_usage, "history": hist, "forecast": forecast},
        )


@app.get("/tenants/expiry", response_class=HTMLResponse)
def tenants_expiry(request: Request, months: int = DEFAULT_EXPIRY_MONTHS, reference: str = "today"):
    with SessionLocal() as session:
        ref = date.today()
        if reference == "latest_report":
            ref = _latest_report_date(session) or ref

        cutoff = ref + relativedelta(months=months)

        expired = session.execute(
            select(Tenant).where(Tenant.expire_date.is_not(None), Tenant.expire_date < ref).order_by(Tenant.expire_date.asc())
        ).scalars().all()

        expiring = session.execute(
            select(Tenant)
            .where(Tenant.expire_date.is_not(None), Tenant.expire_date >= ref, Tenant.expire_date < cutoff)
            .order_by(Tenant.expire_date.asc())
        ).scalars().all()

        return templates.TemplateResponse(
            "tenants_expiry.html",
            {"request": request, "months": months, "reference": reference, "ref": ref, "cutoff": cutoff, "expired": expired, "expiring": expiring},
        )
