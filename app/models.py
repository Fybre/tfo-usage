from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Index

from .db import Base


class ReportUpload(Base):
    __tablename__ = "report_upload"
    id = Column(Integer, primary_key=True)
    uploaded_at = Column(DateTime, nullable=False)
    source_filename = Column(String, nullable=False)
    file_hash = Column(String, nullable=False, unique=True)
    report_date = Column(Date, nullable=False)
    row_count = Column(Integer, nullable=False)


class Tenant(Base):
    __tablename__ = "tenant"
    customer_id = Column(String, primary_key=True)
    tenant_name = Column(String, nullable=True)
    customer_name = Column(String, nullable=True)
    reseller = Column(String, nullable=True)
    creation_date = Column(DateTime, nullable=True)
    expire_date = Column(Date, nullable=True)


Index("ix_tenant_tenant_name", Tenant.tenant_name)
Index("ix_tenant_customer_name", Tenant.customer_name)


class TenantUsage(Base):
    __tablename__ = "tenant_usage"
    id = Column(Integer, primary_key=True)
    report_upload_id = Column(Integer, ForeignKey("report_upload.id"), nullable=False)
    report_date = Column(Date, nullable=False)
    customer_id = Column(String, ForeignKey("tenant.customer_id"), nullable=False)

    used_storage_gb = Column(Float, nullable=True)
    storage_capacity_gb = Column(Float, nullable=True)
    storage_usage_pct = Column(Float, nullable=True)
    exceeded_storage_gb = Column(Float, nullable=True)

    concurrent_users = Column(Integer, nullable=True)
    named_users = Column(Integer, nullable=True)
    read_only_users = Column(Integer, nullable=True)
    document_count = Column(Integer, nullable=True)

    raw_json = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("report_date", "customer_id", name="uq_usage_reportdate_customer"),
        Index("ix_usage_customer_reportdate", "customer_id", "report_date"),
    )
