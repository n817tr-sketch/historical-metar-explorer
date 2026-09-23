from flask import Flask, jsonify, request, render_template, Response, send_file
import csv, io, requests, re
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
CAT = {"VFR": 0, "MVFR": 1, "IFR": 2, "LIFR": 3}


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ceiling(r):
    vals = []
    for i in range(1, 5):
        c = (r.get(f"skyc{i}") or "").upper()
        h = num(r.get(f"skyl{i}"))
        if c in ("BKN", "OVC", "VV") and h is not None:
            vals.append(h)
    return min(vals) if vals else None


def category(v, c):
    vc = "LIFR" if v is not None and v < 1 else "IFR" if v is not None and v < 3 else "MVFR" if v is not None and v <= 5 else "VFR"
    cc = "LIFR" if c is not None and c < 500 else "IFR" if c is not None and c < 1000 else "MVFR" if c is not None and c <= 3000 else "VFR"
    return vc if CAT[vc] >= CAT[cc] else cc


def fetch(icao, date):
    s = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    e = s + timedelta(days=1)
    p = [
        ("station", icao),
        ("data", "tmpf,dwpf,drct,sknt,gust,alti,vsby,skyc1,skyc2,skyc3,skyc4,skyl1,skyl2,skyl3,skyl4,wxcodes,metar"),
        ("sts", s.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("ets", e.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("tz", "UTC"),
        ("format", "onlycomma"),
        ("missing", "null"),
        ("trace", "null"),
        ("report_type", "3"),
        ("report_type", "4"),
    ]
    r = requests.get(IEM, params=p, timeout=45)
    r.raise_for_status()
    out = []
    for x in csv.DictReader(io.StringIO(r.text)):
        if not x.get("valid"):
            continue
        c = ceiling(x)
        v = num(x.get("vsby"))
        raw = x.get("metar") or ""
        out.append({
            "valid": x["valid"],
            "category": category(v, c),
            "tmpf": num(x.get("tmpf")),
            "dwpf": num(x.get("dwpf")),
            "drct": num(x.get("drct")),
            "sknt": num(x.get("sknt")),
            "gust": num(x.get("gust")),
            "vsby": v,
            "ceiling": c,
            "altimeter": num(x.get("alti")),
            "weather": x.get("wxcodes") or "",
            "metar": raw,
            "is_speci": bool(re.search(r"\bSPECI\b", raw)),
        })
    return sorted(out, key=lambda z: z["valid"])


def nearest(obs, iso):
    if not obs:
        return None
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    return min(obs, key=lambda x: abs(datetime.fromisoformat(x["valid"].replace("Z", "+00:00")).timestamp() - t))


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/metar")
def api_metar():
    a = request.args.get("icao", "").strip().upper()
    d = request.args.get("date", "")
    if not re.fullmatch(r"[A-Z0-9]{3,4}", a):
        return jsonify(error="Invalid ICAO."), 400
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        return jsonify(error="Invalid date."), 400
    try:
        o = fetch(a, d)
    except Exception as e:
        return jsonify(error=str(e)), 502
    tr = [{"time": o[i]["valid"], "from": o[i - 1]["category"], "to": o[i]["category"]}
          for i in range(1, len(o)) if o[i]["category"] != o[i - 1]["category"]]
    return jsonify(icao=a, date=d, count=len(o), counts={k: sum(x["category"] == k for x in o) for k in CAT}, transitions=tr, observations=o)


@app.get("/api/route-review")
def route_review():
    airports = [x.strip().upper() for x in request.args.get("airports", "").split(",") if x.strip()][:8]
    date = request.args.get("date", "")
    dep = request.args.get("departure", "")
    try:
        duration = float(request.args.get("duration", "60") or 60)
    except ValueError:
        return jsonify(error="Duration must be a number."), 400
    if len(airports) < 2:
        return jsonify(error="Enter at least two airports."), 400
    try:
        datetime.strptime(date, "%Y-%m-%d")
        start = datetime.fromisoformat(f"{date}T{dep}:00+00:00")
    except ValueError:
        return jsonify(error="Use a valid date and departure time such as 18:30."), 400
    if duration < 1 or duration > 1440:
        return jsonify(error="Duration must be 1–1440 minutes."), 400
    result = []
    for i, a in enumerate(airports):
        try:
            o = fetch(a, date)
            t = start + timedelta(minutes=duration * i / max(1, len(airports) - 1))
            near = nearest(o, t.isoformat().replace("+00:00", "Z"))
            result.append({"icao": a, "target": t.isoformat().replace("+00:00", "Z"), "observation": near})
        except Exception as e:
            result.append({"icao": a, "error": str(e)})
    return jsonify(date=date, departure=dep, duration=duration, airports=result)


@app.get("/api/compare")
def compare():
    arr = [x.strip().upper() for x in request.args.get("airports", "").split(",") if x.strip()][:6]
    d = request.args.get("date", "")
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        return jsonify(error="Invalid date."), 400
    result = {}
    for a in arr:
        try:
            result[a] = fetch(a, d)
        except Exception as e:
            result[a] = {"error": str(e)}
    return jsonify(date=d, airports=result)


@app.get("/api/export")
def export_csv():
    a = request.args.get("icao", "").strip().upper()
    d = request.args.get("date", "")
    try:
        rows = fetch(a, d)
    except Exception as e:
        return jsonify(error=str(e)), 502
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ICAO", "UTC", "Category", "WindDir", "WindKt", "GustKt", "VisibilitySM", "CeilingFt", "TempF", "DewPointF", "Altimeter", "Weather", "METAR"])
    for x in rows:
        w.writerow([a, x["valid"], x["category"], x["drct"], x["sknt"], x["gust"], x["vsby"], x["ceiling"], x["tmpf"], x["dwpf"], x["altimeter"], x["weather"], x["metar"]])
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={a}_{d}_metar.csv"})


@app.get("/api/report")
def generate_report():
    """Generate a printable PDF HEMS historical weather review."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from io import BytesIO

    icao = request.args.get("icao", "").strip().upper()
    date = request.args.get("date", "").strip()
    airports = [x.strip().upper() for x in request.args.get("airports", "").split(",") if x.strip()][:8]
    departure = request.args.get("departure", "")
    duration = request.args.get("duration", "")

    if not re.fullmatch(r"[A-Z0-9]{3,4}", icao) or not date:
        return jsonify(error="icao and date are required"), 400
    try:
        datetime.strptime(date, "%Y-%m-%d")
        reports = fetch(icao, date)
    except Exception as exc:
        return jsonify(error=str(exc)), 502
    if not reports:
        return jsonify(error="No historical observations found"), 404

    transitions = []
    for i in range(1, len(reports)):
        if reports[i]["category"] != reports[i - 1]["category"]:
            transitions.append((reports[i]["valid"], reports[i - 1]["category"], reports[i]["category"]))

    numeric_ceils = [r["ceiling"] for r in reports if isinstance(r.get("ceiling"), (int, float))]
    numeric_vis = [r["vsby"] for r in reports if isinstance(r.get("vsby"), (int, float))]

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, rightMargin=.42 * inch, leftMargin=.42 * inch, topMargin=.42 * inch, bottomMargin=.42 * inch)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("V7Title", parent=styles["Title"], fontSize=18, leading=21, spaceAfter=8)
    body = ParagraphStyle("V7Body", parent=styles["BodyText"], fontSize=9, leading=11)
    small = ParagraphStyle("V7Small", parent=styles["BodyText"], fontSize=7, leading=8.5)
    story = [
        Paragraph(f"HEMS Historical Weather Review — {icao}", title),
        Paragraph(f"Observation date: {date} UTC", body),
        Spacer(1, 5),
        Paragraph("Historical observation review only. This report does not reconstruct aircraft performance, weather between airports, or operational decision-making.", small),
        Spacer(1, 8),
    ]

    summary = [["Metric", "Result"],
               ["Historical reports", str(len(reports))],
               ["Category transitions", str(len(transitions))],
               ["Lowest ceiling", f"{min(numeric_ceils):.0f} ft" if numeric_ceils else "—"],
               ["Lowest visibility", f"{min(numeric_vis):.1f} SM" if numeric_vis else "—"]]
    t = Table(summary, colWidths=[2.0 * inch, 4.75 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222831")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("GRID", (0, 0), (-1, -1), .35, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)
    ]))
    story += [t, Spacer(1, 9)]

    if airports:
        note = f"Airports: {', '.join(airports)}"
        if departure:
            note += f" • Departure: {departure} UTC"
        if duration:
            note += f" • Planned elapsed time: {duration} minutes"
        story += [Paragraph("Route / Flight Review", styles["Heading2"]), Paragraph(note, body), Spacer(1, 6)]

    if transitions:
        story.append(Paragraph("Flight Category Transitions", styles["Heading2"]))
        rows = [["UTC", "From", "To"]] + [[str(ts), a, b] for ts, a, b in transitions]
        tt = Table(rows, colWidths=[2.2 * inch, 1.4 * inch, 1.4 * inch])
        tt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222831")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("GRID", (0, 0), (-1, -1), .35, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.whitesmoke])
        ]))
        story += [tt, Spacer(1, 9)]

    story.append(Paragraph("Historical Observation Log", styles["Heading2"]))
    rows = [["UTC", "Cat", "Wind", "Vis", "Ceil", "Alt", "Wx", "SPECI"]]
    for r in reports:
        wind = ""
        if r.get("drct") is not None and r.get("sknt") is not None:
            wind = f'{r["drct"]:03.0f}/{r["sknt"]:.0f}'
            if r.get("gust") is not None:
                wind += f'G{r["gust"]:.0f}'
        vis = "" if r.get("vsby") is None else f'{r["vsby"]:.1f}'
        ceil = "CLR" if r.get("ceiling") is None else f'{r["ceiling"]:.0f}'
        alt = "" if r.get("altimeter") is None else f'{r["altimeter"]:.2f}'
        wx = (r.get("weather") or "")[:22]
        rows.append([r["valid"][11:16] + "Z", r["category"], wind, vis, ceil, alt, wx, "YES" if r.get("is_speci") else ""])
    ot = Table(rows, colWidths=[.55*inch, .45*inch, .78*inch, .42*inch, .55*inch, .55*inch, 1.65*inch, .45*inch], repeatRows=1)
    ot.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#222831")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("GRID", (0, 0), (-1, -1), .25, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 6.5), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
    ]))
    for idx, r in enumerate(reports, start=1):
        bg = {"VFR": colors.HexColor("#d9f2df"), "MVFR": colors.HexColor("#dbeafe"), "IFR": colors.HexColor("#fee2e2"), "LIFR": colors.HexColor("#f5d6ef")}[r["category"]]
        ot.setStyle(TableStyle([("BACKGROUND", (1, idx), (1, idx), bg), ("FONTNAME", (1, idx), (1, idx), "Helvetica-Bold")]))
    story += [ot, Spacer(1, 8), Paragraph("Source: Iowa Environmental Mesonet historical ASOS/METAR archive. Times shown in UTC.", small)]

    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=f"{icao}_{date}_HEMS_weather_review.pdf")


def great_circle_nm(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, asin, sqrt
    r = 3440.065
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * r * asin(sqrt(a))


@app.get("/api/route-review-tas")
def route_review_tas():
    airports = [
        x.strip().upper()
        for x in request.args.get("airports", "").split(",")
        if x.strip()
    ][:8]

    date = request.args.get("date", "")
    dep = request.args.get("departure", "")

    try:
        tas = float(request.args.get("tas", "160") or 160)
    except ValueError:
        return jsonify(error="True airspeed must be a number."), 400

    if len(airports) < 2:
        return jsonify(error="Enter at least two airports."), 400

    if tas < 40 or tas > 250:
        return jsonify(error="True airspeed must be between 40 and 250 kt."), 400

    if not re.fullmatch(r"\d{2}:\d{2}", dep or ""):
        return jsonify(error="Use a Zulu departure time such as 18:00."), 400

    try:
        datetime.strptime(date, "%Y-%m-%d")
        hh, mm = [int(x) for x in dep.split(":")]

        if hh > 23 or mm > 59:
            raise ValueError

        start = datetime.strptime(
            f"{date} {dep}",
            "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)

    except ValueError:
        return jsonify(
            error="Use a valid date and Zulu departure time such as 18:00."
        ), 400

    coords = {
        "KTLH": (30.3965, -84.3503),
        "KABY": (31.5355, -84.1945),
        "KDHN": (31.3213, -85.4496),
        "KJAX": (30.4941, -81.6879),
        "KATL": (33.6407, -84.4277),
        "KMCO": (28.4294, -81.3089),
    }

    missing = [a for a in airports if a not in coords]

    if missing:
        return jsonify(
            error=f"No route coordinates are configured for: {', '.join(missing)}."
        ), 400

    legs = []
    elapsed = 0.0

    for i in range(len(airports) - 1):
        a = airports[i]
        b = airports[i + 1]

        nm = great_circle_nm(
            *coords[a],
            *coords[b]
        )

        minutes = nm / tas * 60.0
        elapsed += minutes

        target = start + timedelta(minutes=elapsed)

        legs.append({
            "from": a,
            "to": b,
            "distance_nm": round(nm, 1),
            "minutes": round(minutes, 1),
            "target": target.isoformat().replace("+00:00", "Z")
        })

    result = []

    for i, a in enumerate(airports):

        if i == 0:
            target = start
        else:
            target = start + timedelta(
                minutes=sum(x["minutes"] for x in legs[:i])
            )

        target_date = target.strftime("%Y-%m-%d")

        try:
            obs = fetch(a, target_date)

            near = nearest(
                obs,
                target.isoformat().replace("+00:00", "Z")
            )

            result.append({
                "icao": a,
                "target": target.isoformat().replace("+00:00", "Z"),
                "observation": near
            })

        except Exception as e:
            result.append({
                "icao": a,
                "target": target.isoformat().replace("+00:00", "Z"),
                "error": str(e)
            })

    return jsonify(
        date=date,
        departure=dep,
        tas=tas,
        duration=round(elapsed, 1),
        legs=legs,
        airports=result
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
