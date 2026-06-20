from io import BytesIO

from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _badge_text(ctx: dict) -> str:
    badges = []
    if ctx["over_capacity"]:
        badges.append("OVER CAPACITY")
    if ctx["eta_within_threshold"]:
        badges.append(f"Storage ETA ≤ {ctx['imminent_days']}d")
    if ctx["expired"]:
        badges.append("SUPPORT EXPIRED")
    if ctx["expiry_within_threshold"]:
        badges.append(f"Support Expiry ≤ {ctx['imminent_days']}d")
    return "  |  ".join(badges) if badges else "No active flags"


def _build_chart(history) -> Drawing | None:
    hist = [h for h in history if h.report_date]
    if len(hist) < 2:
        return None

    used = [h.used_storage_gb or 0 for h in hist]
    capacity = [h.storage_capacity_gb or 0 for h in hist]
    labels = [h.report_date.strftime("%Y-%m-%d") for h in hist]

    drawing = Drawing(480, 220)
    chart = HorizontalLineChart()
    chart.x = 50
    chart.y = 40
    chart.height = 150
    chart.width = 380
    chart.data = [used, capacity]
    chart.lines[0].strokeColor = colors.HexColor("#0d6efd")
    chart.lines[1].strokeColor = colors.HexColor("#6c757d")
    chart.lines[0].strokeWidth = 2
    chart.lines[1].strokeWidth = 2

    chart.categoryAxis.categoryNames = labels
    chart.categoryAxis.labels.angle = 45
    chart.categoryAxis.labels.dx = -8
    chart.categoryAxis.labels.dy = -10
    chart.categoryAxis.labels.fontSize = 6

    max_val = max(used + capacity) if (used + capacity) else 1
    chart.valueAxis.valueMin = 0
    chart.valueAxis.valueMax = max_val * 1.1 if max_val else 1

    step = max(1, len(labels) // 12)
    chart.categoryAxis.labels.boxAnchor = "n"
    chart.categoryAxis.visibleTicks = True
    if step > 1:
        chart.categoryAxis.labels.dx = -8

    drawing.add(chart)

    legend = Legend()
    legend.x = 50
    legend.y = 195
    legend.dx = 8
    legend.dy = 8
    legend.fontSize = 8
    legend.alignment = "right"
    legend.columnMaximum = 1
    legend.colorNamePairs = [
        (colors.HexColor("#0d6efd"), "Used (GB)"),
        (colors.HexColor("#6c757d"), "Capacity (GB)"),
    ]
    drawing.add(legend)

    return drawing


def build_tenant_report_pdf(ctx: dict) -> bytes:
    tenant = ctx["tenant"]
    latest_usage = ctx["latest_usage"]
    history = ctx["history"]
    forecast = ctx["forecast"]

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph(tenant.customer_name or tenant.tenant_name or tenant.customer_id, styles["Title"]))
    elements.append(Paragraph(f"Tenant: {tenant.tenant_name or tenant.customer_id}", styles["Heading3"]))
    elements.append(Spacer(1, 0.3 * cm))
    elements.append(Paragraph(_badge_text(ctx), styles["Heading4"]))
    elements.append(Spacer(1, 0.5 * cm))

    summary_rows = [["Field", "Value"]]
    summary_rows.append(["Customer ID", tenant.customer_id or "N/A"])
    summary_rows.append(["Reseller", tenant.reseller or "N/A"])
    summary_rows.append(["Latest date", str(latest_usage.report_date) if latest_usage else "N/A"])
    summary_rows.append(["Expire date", str(tenant.expire_date) if tenant.expire_date else "N/A"])
    summary_rows.append(["Creation date", str(tenant.creation_date.date()) if tenant.creation_date else "N/A"])
    if latest_usage:
        summary_rows.append(["Concurrent users", str(latest_usage.concurrent_users) if latest_usage.concurrent_users is not None else "N/A"])
        summary_rows.append(["Named users", str(latest_usage.named_users) if latest_usage.named_users is not None else "N/A"])
        summary_rows.append(["Read only users", str(latest_usage.read_only_users) if latest_usage.read_only_users is not None else "N/A"])
        summary_rows.append(["Documents", str(latest_usage.document_count) if latest_usage.document_count is not None else "N/A"])
        summary_rows.append(["Used", f"{latest_usage.used_storage_gb or 0:.2f} GB"])
        summary_rows.append(["Capacity", f"{latest_usage.storage_capacity_gb or 0:.1f} GB"])
        summary_rows.append(["Usage", f"{ctx['latest_usage_pct'] or 0:.2f}%"])
    if forecast and forecast.growth_gb_per_day is not None:
        summary_rows.append(["Growth", f"~{forecast.growth_gb_per_day * 30:.2f} GB/month"])
        summary_rows.append(["Est. days to full", f"{forecast.days_to_full:.1f}" if forecast.days_to_full is not None else "N/A"])
    summary_rows.append(["Est. full date", str(ctx["est_full_date"]) if ctx["est_full_date"] else "N/A"])
    summary_rows.append(["Days to expiry", str(ctx["days_to_expiry"]) if ctx["days_to_expiry"] is not None else "N/A"])

    summary_table = Table(summary_rows, colWidths=[5 * cm, 10 * cm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#212529")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dee2e6")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f9fa")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 0.7 * cm))

    chart = _build_chart(history)
    if chart:
        elements.append(Paragraph("Storage over time", styles["Heading3"]))
        elements.append(Spacer(1, 0.2 * cm))
        elements.append(chart)
        elements.append(Spacer(1, 0.7 * cm))

    elements.append(Paragraph("Records", styles["Heading3"]))
    elements.append(Spacer(1, 0.2 * cm))

    record_rows = [["Date", "Used (GB)", "Capacity (GB)", "Usage (%)", "Concurrent", "Named", "Read Only", "Docs"]]
    for h in reversed(history):
        pct = ((h.used_storage_gb or 0) / h.storage_capacity_gb * 100) if h.storage_capacity_gb else (h.storage_usage_pct or 0)
        record_rows.append([
            str(h.report_date),
            f"{h.used_storage_gb or 0:.2f}",
            f"{h.storage_capacity_gb or 0:.1f}",
            f"{pct:.2f}",
            str(h.concurrent_users) if h.concurrent_users is not None else "",
            str(h.named_users) if h.named_users is not None else "",
            str(h.read_only_users) if h.read_only_users is not None else "",
            str(h.document_count) if h.document_count is not None else "",
        ])

    records_table = Table(record_rows, repeatRows=1)
    records_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#212529")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dee2e6")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f9fa")]),
    ]))
    elements.append(records_table)

    doc.build(elements)
    return buf.getvalue()
