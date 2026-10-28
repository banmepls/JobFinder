"""Default CV document - the user's résumé recreated as editable HTML.

Rendered inside the .cv template (see templates/cv.html) which reproduces the
original layout: name with a heavy rule, uppercase section headers with a thin
rule, experience entries with a right-aligned italic date, and bullet lists.
This HTML is the initial content of profile.cv_html; the user edits it in place,
and the server renders it to a real downloadable PDF with the same CSS.
"""

def build_cv_css(base_px: float = 12.5) -> str:
    """Résumé (.cv-doc) CSS at a given base font size. Everything scales in `em`
    off `base_px`, so shrinking the base tightens both text and spacing - used by
    the PDF auto-fit loop to keep the CV on a single page."""
    return f"""
.cv-doc {{ font-family: Calibri, Carlito, "Segoe UI", Helvetica, Arial, sans-serif;
          color:#111; font-size:{base_px}px; line-height:1.4; }}
.cv-doc .cv-name {{ font-size:1.76em; font-weight:700; letter-spacing:.5px;
                   border-bottom:.18em solid #111; padding-bottom:.4em; margin-bottom:.55em; }}
.cv-doc .cv-subtitle {{ font-size:1em; }}
.cv-doc .cv-contact {{ font-size:1em; margin-top:.1em; }}
.cv-doc h2 {{ font-size:1em; font-weight:700; text-transform:uppercase; letter-spacing:.4px;
             border-bottom:1px solid #111; padding-bottom:.15em; margin:1.1em 0 .45em; }}
.cv-doc .entry {{ margin-bottom:.6em; }}
.cv-doc .entry-head {{ display:flex; justify-content:space-between; gap:1em; }}
.cv-doc .entry-head .date {{ font-style:italic; white-space:nowrap; }}
.cv-doc .entry-desc {{ font-style:italic; }}
.cv-doc ul {{ margin:.2em 0 0; padding-left:1.4em; }}
.cv-doc li {{ margin-bottom:.22em; }}
.cv-doc p {{ margin:0; }}
.cv-doc a {{ color:inherit; text-decoration:underline; }}
.cv-doc .cv-li {{ font-size:.7em; font-weight:700; color:#0a66c2; text-decoration:none;
                 border:1px solid #0a66c2; border-radius:3px; padding:0 3px; vertical-align:middle; }}
""".strip()


# Screen default (unchanged look for the editor).
CV_DOC_CSS = build_cv_css(12.5)


def cv_to_text(cv_html: str) -> str:
    """Plain-text version of the WHOLE CV document (headline, summary, section
    headers, each entry's heading, skills and bullets), used as the résumé
    everywhere the AI needs it. The CV document is the single source of truth
    for the résumé.

    The entry heading - role, company, place and dates - is a div, not a p or
    li, and was skipped until 28 Oct 2026: the AI got every bullet as one list
    with no job title, employer or date, while the page showed the CV whole."""
    from bs4 import BeautifulSoup

    def norm(el) -> str:
        return " ".join(el.get_text(" ").split())

    soup = BeautifulSoup(cv_html or "", "html.parser")
    lines: list[str] = []
    sub = soup.select_one(".cv-subtitle")
    if sub and norm(sub):
        lines.append(f"HEADLINE: {norm(sub)}")
    for el in soup.find_all(["h2", "p", "li", "div"]):
        if el.name == "div" and "entry-head" not in (el.get("class") or []):
            continue
        if el.name == "div":
            # Dates sit in their own span; set them off so they read as dates.
            date = el.select_one(".date")
            when = norm(date) if date else ""
            if date:
                date.decompose()
            what = norm(el)
            txt = f"{what} | {when}" if what and when else what or when
        else:
            txt = norm(el)
        if not txt:
            continue
        if el.name == "h2":
            lines.append(f"\n{txt.upper()}")
        elif el.name == "li":
            lines.append(f"- {txt}")
        elif el.name == "div":  # entry heading: a blank line before each job
            lines.append(f"\n{txt}")
        else:  # summary paragraph, or an entry's one-line description
            lines.append(txt)
    return "\n".join(lines).strip()
