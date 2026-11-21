"""PDF research report (Morningstar-style layout) from report.build(). Pure Python (fpdf2), so it runs on Vercel.

Sections: header strip (price, fair value, stars, uncertainty, moat, capital allocation), executive summary,
price vs fair value chart, investment thesis sections, moat by the numbers, financial model, DCF breakdown,
competitors, capital allocation, risks, bull and bear tests, notes archive.
"""
import datetime as dt
import re

from fpdf import FPDF

REPL = {"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...",
        "•": "-", "→": "->", "≥": ">=", "≤": "<=", "×": "x", "−": "-", "≈": "~",
        "★": "*", "☆": "-", " ": " ", " ": " ", " ": " ", "‑": "-", "·": "|"}
INK, MUTED, ACC, LINE = (20, 20, 20), (110, 110, 110), (200, 120, 20), (210, 210, 210)


def clean(v):
    """fpdf2's core fonts are Latin-1: swap common typography and drop anything else."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        v = "\n".join("- " + clean(x) for x in v)
    elif isinstance(v, dict):
        v = "; ".join(f"{k.replace('_', ' ')}: {clean(x)}" for k, x in v.items() if x not in (None, "", [], {}))
    s = str(v)
    for a, b in REPL.items():
        s = s.replace(a, b)
    return s.encode("latin-1", "ignore").decode("latin-1")


def _m(v):
    return "-" if v is None else (f"${v:,.0f}" if abs(v) >= 1000 else f"${v:,.2f}")


def _p(v):
    return "-" if v is None else f"{v * 100:.1f}%"


def _mm(v):
    return "-" if v is None else f"{v / 1e6:,.0f}"


def _x(v):
    return "-" if v is None else f"{v:.1f}"


class Report(FPDF):
    def __init__(self, rep):
        super().__init__(format="A4")
        self.rep = rep
        self.set_auto_page_break(True, 16)
        self.set_margins(14, 14, 14)

    def header(self):
        r = self.rep
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*MUTED)
        self.cell(0, 4, clean(f"ValueInvestor equity research | {r['ticker']} | report as of {r['generated']} | USD"), new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(*INK)
        st = (r.get("stars") or {}).get("stars")
        nm = r["name"] if r.get("name") and r["name"] != r["ticker"] else ""
        self.cell(0, 8, clean(f"{nm}  {r['ticker']}  " + ("*" * st if st else "")).strip(), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*LINE)
        self.line(14, self.get_y(), 196, self.get_y())
        self.ln(2)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*MUTED)
        self.cell(0, 4, clean("Informational only, not investment advice. Values computed by the ValueInvestor engine from Claude's "
                              f"assumptions; claims graded A-E in the app.   Page {self.page_no()}"), align="C")

    # ---- building blocks
    def heading(self, text):
        if self.get_y() > 255:
            self.add_page()
        self.ln(2)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*ACC)
        self.cell(0, 6, clean(text), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*INK)

    def para(self, text, size=9, bold_lead=None):
        if not text:
            return
        self.set_font("Helvetica", "", size)
        if bold_lead:
            self.set_font("Helvetica", "B", size)
            self.write(4.6, clean(bold_lead) + " ")
            self.set_font("Helvetica", "", size)
        self.multi_cell(0, 4.6, clean(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def grid(self, head, rows, widths=None, size=7.5, first_bold=True):
        if not rows:
            return
        self.set_font("Helvetica", "", size)
        w = widths or [182 / len(head)] * len(head)
        with self.table(col_widths=w, text_align=["LEFT"] + ["RIGHT"] * (len(head) - 1), line_height=4.2,
                        borders_layout="HORIZONTAL_LINES", first_row_as_headings=True) as t:
            rr = t.row()
            for c in head:
                rr.cell(clean(c))
            for row in rows:
                rr = t.row()
                for c in row:
                    rr.cell(clean(c))
        self.ln(2)

    def tiles(self, items):
        x0, y0, w = 14, self.get_y(), 182 / len(items)
        for i, (k, v) in enumerate(items):
            self.set_xy(x0 + i * w, y0)
            self.set_font("Helvetica", "", 7)
            self.set_text_color(*MUTED)
            self.cell(w, 4, clean(k))
            self.set_xy(x0 + i * w, y0 + 4)
            self.set_font("Helvetica", "B", 10)
            self.set_text_color(*INK)
            self.cell(w, 6, clean(v))
        self.set_xy(14, y0 + 12)

    def chart(self, rep, h=48):
        pts = [(d, p) for d, p in rep.get("prices") or [] if p]
        if len(pts) < 5:
            return
        fvh = rep.get("fv_history") or []
        vals = [p for _, p in pts] + [f for _, f in fvh]
        bands = (rep.get("uncertainty") or {}).get("bands")
        if fvh and bands:
            vals += [fvh[-1][1] * bands[0], fvh[-1][1] * bands[1]]
        lo, hi = min(vals) * 0.95, max(vals) * 1.05
        x0, y0, w = 26, self.get_y() + 2, 168
        d0 = dt.date.fromisoformat(pts[0][0])
        span = max((dt.date.fromisoformat(pts[-1][0]) - d0).days, 1)
        X = lambda d: x0 + (dt.date.fromisoformat(max(d, pts[0][0])) - d0).days / span * w
        Y = lambda v: y0 + (hi - v) / (hi - lo) * h
        self.set_font("Helvetica", "", 6.5)
        self.set_text_color(*MUTED)
        self.set_draw_color(*LINE)
        for k in range(5):
            v = lo + (hi - lo) * k / 4
            self.line(x0, Y(v), x0 + w, Y(v))
            self.set_xy(12, Y(v) - 2)
            self.cell(13, 4, f"{v:,.0f}", align="R")
        if fvh and bands:
            last = fvh[-1][1]
            self.set_fill_color(250, 236, 210)
            xs = X(fvh[-1][0])
            self.rect(xs, Y(last * bands[1]), x0 + w - xs, Y(last * bands[0]) - Y(last * bands[1]), style="F")
        self.set_draw_color(40, 120, 220)
        self.set_line_width(0.35)
        for (d1, p1), (d2, p2) in zip(pts, pts[1:]):
            self.line(X(d1), Y(p1), X(d2), Y(p2))
        if fvh:
            self.set_draw_color(*ACC)
            self.set_line_width(0.6)
            seq = fvh + [(pts[-1][0], fvh[-1][1])]
            for (d1, f1), (d2, f2) in zip(seq, seq[1:]):
                self.line(X(d1), Y(f1), X(d2), Y(f1))
                self.line(X(d2), Y(f1), X(d2), Y(f2))
        self.set_line_width(0.2)
        for i in (0, len(pts) // 2, len(pts) - 1):
            self.set_xy(X(pts[i][0]) - 8, y0 + h + 1)
            self.cell(16, 3, pts[i][0][:7], align="C")
        self.set_xy(14, y0 + h + 5)
        self.set_text_color(*MUTED)
        self.cell(0, 4, "Blue: price. Orange: Claude's fair value (steps at each thesis version). Shaded: 5-star to 1-star band.",
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*INK)


SECTION_ORDER = [("Business", "business"), ("Core technology and innovation", "technology"),
                 ("Strategy and outlook: partnerships, backlog, diversification", "strategic_outlook"),
                 ("Economic moat", "moat"), ("Right to win", "right_to_win"), ("Market size and competitors", "tam_competitors"),
                 ("Latest quarter: GAAP vs non-GAAP", "latest_quarter"), ("Financial quality", "financials"),
                 ("Variant view", "variant_view"), ("Fair value and profit drivers", "valuation"),
                 ("Bulls say / bears say: what would prove each", "bull_bear_tests"),
                 ("Risk and uncertainty", "company_risks"), ("Controversy and short-seller view", "controversy"),
                 ("Industry outlook", "outlook"), ("Management", "management"), ("Pillars and kill criteria", "pillars"),
                 ("Catalysts and flows", "catalysts_flow"), ("Pre-mortem", "premortem")]


def build_pdf(rep):
    pdf = Report(rep)
    pdf.add_page()
    st, u, ca = rep.get("stars") or {}, rep.get("uncertainty") or {}, rep.get("capital_allocation") or {}
    th = rep.get("thesis") or {}
    pdf.tiles([("Last price", f"{_m(rep.get('price'))}"), ("Fair value", _m(rep.get("fair_value"))),
               ("Price/FV", _x(st.get("p_fv"))), ("Rating", ("*" * st["stars"]) if st.get("stars") else "-"),
               ("Uncertainty", u.get("rating") or "-"), ("Moat", (rep.get("moat_width") or "not rated").title()),
               ("Capital alloc.", ca.get("rating") or "-")])
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MUTED)
    line = (f"{rep.get('sector') or ''} | {rep.get('industry') or ''} | thesis v{th.get('version', '-')} "
            f"{str(th.get('created_at') or '')[:10]} ({th.get('approval') or '-'}) | verdict {th.get('verdict') or '-'} | "
            f"conviction {int(float(th['conviction'])) if th.get('conviction') is not None else '-'} | entry target {_m(rep.get('entry_target'))}")
    pdf.multi_cell(0, 4, clean(line), new_x="LMARGIN", new_y="NEXT")
    if st.get("stars"):
        pdf.multi_cell(0, 4, clean(f"Star bands ({u.get('rating')} uncertainty): 5 stars at or below {_m(st['five_star'])}; "
                                   f"1 star at or above {_m(st['one_star'])}. {u.get('why') or ''}"), new_x="LMARGIN", new_y="NEXT")
    if not rep.get("has_assumptions"):
        pdf.multi_cell(0, 4, clean(f"Note: model built from {rep.get('basis')}."), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)
    es = rep.get("exec") or {}
    notes = rep.get("notes") or []
    pdf.heading(clean((notes[0]["title"] if notes and notes[0].get("title") else "") or "Executive summary"))
    if notes and notes[0].get("why_it_matters"):
        pdf.para(notes[0]["why_it_matters"], bold_lead="Why it matters:")
    pdf.para(es.get("one_line"), bold_lead=f"{es.get('recommendation') or ''}:" if es.get("recommendation") else None)
    if es.get("why"):
        pdf.para(es["why"], bold_lead="Why:")
    if es.get("key_risks"):
        kr = es["key_risks"] if isinstance(es["key_risks"], list) else [es["key_risks"]]
        pdf.para([f"{k.get('risk')} (watch: {k.get('watch')})" if isinstance(k, dict) else k for k in kr], bold_lead="Key risks:")
    if isinstance(es.get("plan"), dict):
        pdf.para(es["plan"], bold_lead="Plan:")
    if notes and notes[0].get("bottom_line"):
        pdf.para(notes[0]["bottom_line"], bold_lead="Bottom line:")
    pdf.heading("Price vs fair value")
    pdf.chart(rep)
    secs = rep.get("sections") or {}
    mm = rep.get("moat_metrics") or []
    for title, key in SECTION_ORDER:
        body = secs.get(key)
        if key == "moat":
            pdf.heading("Economic moat")
            pdf.para(body or "Not written yet.")
            if mm:
                pdf.grid(["Metric", rep["ticker"], "Peers", "What it shows", "Grade"],
                          [[m.get("metric"), m.get("company"), "; ".join(f"{k} {v}" for k, v in (m.get("peers") or {}).items()),
                            m.get("reads"), f"{m.get('grade') or ''} {m.get('source') or ''}"] for m in mm if isinstance(m, dict)],
                          widths=[34, 30, 40, 54, 24], first_bold=False)
            continue
        if key == "valuation":
            pdf.heading(title)
            pdf.para(body)
            _model(pdf, rep)
            continue
        if body:
            pdf.heading(title)
            pdf.para(body)
        if key == "company_risks" and u.get("rating"):
            pdf.para(f"{u['rating']} uncertainty ({u.get('why')}).", size=8)
    _peers(pdf, rep)
    if ca:
        pdf.heading(f"Capital allocation: {ca.get('rating') or '-'}")
        for k, lab in (("balance_sheet", "Balance sheet"), ("investment_efficiency", "Investment efficiency"),
                       ("distributions", "Shareholder distributions")):
            it = ca.get(k)
            if it:
                pdf.para(it.get("text") if isinstance(it, dict) else it,
                         bold_lead=f"{lab}{(' (' + it['rating'] + ')') if isinstance(it, dict) and it.get('rating') else ''}:")
        if ca.get("governance"):
            pdf.para(ca["governance"], bold_lead="Governance:")
    if notes:
        pdf.heading("Research notes archive")
        for nt in notes:
            fv = (f"fair value {_m(nt['fv_prev'])} -> {_m(nt['fv'])}" if nt.get("fv_prev") and nt.get("fv") and nt["fv_prev"] != nt["fv"]
                  else f"fair value {_m(nt.get('fv'))}")
            pdf.para(f"v{nt['version']} | {nt['date']} | {nt.get('verdict') or ''} | {fv}", size=8, bold_lead=nt.get("title"))
            if nt.get("bottom_line") and nt is not notes[0]:
                pdf.para(nt["bottom_line"], size=8)
    return bytes(pdf.output())


def _model(pdf, rep):
    pj, d = rep.get("projection"), rep.get("dcf")
    if not pj:
        if rep.get("engine_note"):
            pdf.para(rep["engine_note"], size=8)
        return
    acts, rows = rep.get("actuals") or [], pj["rows"][:5]
    ya = acts[-1]["year"] if acts else None
    head = [""] + [f"{h['year']}A" for h in acts] + [f"{(ya + r['year']) if ya else 'Y' + str(r['year'])}E" for r in rows]

    def line(lab, fa, fe):
        return [lab] + [fa(h) for h in acts] + [fe(r) for r in rows]
    tbl = [line("Revenue ($M)", lambda h: _mm(h.get("revenue")), lambda r: _mm(r["revenue"])),
           line("Revenue growth", lambda h: _p(h.get("rev_growth")), lambda r: _p(r["growth"])),
           line("Operating margin", lambda h: _p(h.get("op_margin")), lambda r: _p(r["op_margin"])),
           line("Net income / after-tax op. profit", lambda h: _mm(h.get("net_income")), lambda r: _mm(r["nopat"])),
           line("Free cash flow ($M)", lambda h: _mm(h.get("fcf")), lambda r: _mm(r["fcff"])),
           line("Diluted shares (M)", lambda h: _mm(h.get("shares")), lambda r: _mm(r["shares"])),
           line("EPS / operating EPS", lambda h: f"{h['eps']:.2f}" if h.get("eps") is not None else "-", lambda r: f"{r['eps']:.2f}"),
           line("ROIC / return on new inv.", lambda h: _p(h.get("roic")), lambda r: _p(r.get("roni"))),
           line("P/E at today's price", lambda h: _x(rep["price"] / h["eps"]) if h.get("eps") and h["eps"] > 0 and rep.get("price") else "-",
                lambda r: _x(r.get("pe"))),
           line("EV/EBIT at today's price", lambda h: "-", lambda r: _x(r.get("ev_ebit")))]
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 5, "Financial model (base case)", new_x="LMARGIN", new_y="NEXT")
    pdf.grid(head, tbl, widths=[46] + [17] * (len(head) - 1))
    s = pj["summary"]
    pdf.para(f"10-year growth a year: revenue {_p(s.get('rev_cagr_10y'))}, operating profit {_p(s.get('ebit_cagr_10y'))}, "
             f"operating EPS {_p(s.get('eps_cagr_9y'))}.", size=8)
    if d:
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "DCF breakdown and key drivers", new_x="LMARGIN", new_y="NEXT")
        dr = d["drivers"]
        pdf.grid(["Item", "$M", "Driver", "Value"],
                  [["PV years 1-5", _mm(d["pv_stage1"]), "Discount rate", _p(dr["discount_rate"])],
                   ["PV years 6-10", _mm(d["pv_stage2"]), "Terminal growth", _p(dr["terminal_growth"])],
                   [f"PV terminal ({d['terminal_how']})", _mm(d["pv_terminal"]), "Tax rate", _p(dr["tax"])],
                   ["Firm value", _mm(d["firm_value"]), "Reinvestment / revenue", _p(dr["reinvest_pct"])],
                   ["Less net debt", _mm(-d["net_debt"]), "Years 6-10 growth", _p(dr["stage2_growth"])],
                   ["Equity value", _mm(d["equity_value"]), "Op. margin year 10", _p(dr["op_margin_y10"])],
                   ["Shares, year 5 (M)", _mm(d["shares"]), "Share change a year", _p(dr["share_change"])],
                   ["Value per share", _m(d["value_per_share"]), "Terminal share of value", _p(d["terminal_share"])]],
                  widths=[58, 33, 58, 33])
    if rep.get("revisions"):
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, clean(f"Forecast revisions vs {rep.get('revisions_since')}"), new_x="LMARGIN", new_y="NEXT")
        rv = rep["revisions"]
        pdf.grid(["", *[f"Y{x['year']} now" for x in rv], *[f"Y{x['year']} prior" for x in rv]],
                  [["Revenue ($M)", *[_mm(x["revenue"][0]) for x in rv], *[_mm(x["revenue"][1]) for x in rv]],
                   ["Operating EPS", *[f"{x['eps'][0]:.2f}" for x in rv], *[f"{x['eps'][1]:.2f}" for x in rv]]])


def _peers(pdf, rep):
    pt = [p for p in rep.get("peer_table") or [] if isinstance(p, dict)]
    if not pt:
        return
    pdf.heading("Competitors")
    rows = []
    for p in pt:
        fv = (rep.get("peer_fv") or {}).get(p.get("ticker"))
        rows.append([f"{p.get('ticker')} {p.get('name') or ''}"[:28], (p.get("moat") or "-").title(), _m(p.get("price")),
                     (_m(fv) if fv else "-"), _x(p.get("pe")), _x(p.get("ps")), _x(p.get("pb")), _p(p.get("div_yield")),
                     f"${p['mcap_bn']:.0f}B" if p.get("mcap_bn") is not None else "-",
                     f"{_m(p.get('low_52w'))}-{_m(p.get('high_52w'))}" if p.get("low_52w") else "-", _p(p.get("roic"))])
    pdf.grid(["Company", "Moat", "Price", "Claude FV", "P/E", "P/S", "P/B", "Div", "Mkt cap", "52-week", "ROIC"], rows,
              widths=[34, 14, 15, 15, 11, 11, 11, 12, 15, 28, 16])
