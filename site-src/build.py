"""Build site/index.html from index.src.html.

Inlines the icons and brand marks, and draws the hero chart from the engine's own numbers, so the
page carries no hand-typed coordinates. Run: python site-src/build.py. Upload site/, never this one.

Tokens in index.src.html:
  {{icon:NAME}}   site-src/icons/solar-NAME.svg (Solar, CC BY 4.0, credited in site/licenses/)
  {{logo:NAME}}   site-src/icons/NAME.svg (Iconify "logos" set, CC0; marks belong to their owners)
  {{chart:KEY}}   geometry computed below
  {{link:KEY}}    mail links (unused since the page became the CLI home page)
"""
import re
from itertools import count
from pathlib import Path

HERE = Path(__file__).parent
SITE = HERE.parent / "site"

_BODY = "Team%3A%0D%0AWhere%20our%20Claude%20Code%20telemetry%20goes%20(Grafana%20Cloud%2C%20Datadog%2C%20or%20not%20yet)%3A%0D%0A"
LINKS = {
    "mailto": "mailto:hello@mycroftcompute.com?subject=Free%20Claude%20Code%20spend%20audit&amp;body=" + _BODY,
    "gmail": "https://mail.google.com/mail/?view=cm&amp;fs=1&amp;to=hello%40mycroftcompute.com"
             "&amp;su=Free%20Claude%20Code%20spend%20audit&amp;body=Team%3A%0AWhere%20our%20Claude%20Code%20"
             "telemetry%20goes%20(Grafana%20Cloud%2C%20Datadog%2C%20or%20not%20yet)%3A%0A",
    "outlook": "https://outlook.office.com/mail/deeplink/compose?to=hello%40mycroftcompute.com"
               "&amp;subject=Free%20Claude%20Code%20spend%20audit",
    "check": "mailto:hello@mycroftcompute.com?subject=Our%20cache-write%20share&amp;body="
             "Our%20number%3A%0D%0AFive-minute%20or%20one-hour%20rate%3A%0D%0AGrafana%20or%20Datadog%3A%0D%0A",
}

# Input cost of one coding-agent step (about 141k cached prompt tokens and 1k fresh ones) at list
# prices. From worker/src/mycroft/economics/caching.py and the README table.
STAY = 0.045276        # Sonnet 4.6, cache warm
SWITCH_FIRST = 0.177242  # first request on Haiku 4.5, writing its cache
SWITCH_WARM = 0.015092   # every request after that on Haiku 4.5
ALTERNATE = 0.425976     # alternating per request, neither cache warm

# Plot box inside the SVG viewBox (0 0 720 400): requests 0..10 across, $0..$0.50 up.
X0, X1, Y0, YTOP, VMAX, N = 64, 696, 352, 40, 0.50, 10


def x(n: float) -> float:
    return X0 + (X1 - X0) * n / N


def y(v: float) -> float:
    return Y0 - (Y0 - YTOP) * v / VMAX


def stay(n: int) -> float:
    return STAY * n


def switch(n: int) -> float:
    return 0.0 if n == 0 else SWITCH_FIRST + SWITCH_WARM * (n - 1)


def path(points) -> str:
    return "M" + " L".join(f"{px:.1f} {py:.1f}" for px, py in points)


def chart() -> dict[str, str]:
    c = {
        "stay": path((x(n), y(stay(n))) for n in range(N + 1)),
        "switch": path((x(n), y(switch(n))) for n in range(N + 1)),
    }
    # The alternating line leaves the scale after one request; draw it to just above the top.
    exit_v = VMAX + 0.035
    exit_n = 1 + (exit_v - ALTERNATE) / ALTERNATE
    c["alt"] = path([(x(0), y(0)), (x(1), y(ALTERNATE)), (x(exit_n), y(exit_v))])
    c["alt-x"] = f"{x(exit_n):.1f}"
    c["alt-y"] = f"{y(exit_v):.1f}"
    grid = []
    for i in range(0, 6):
        v = i / 10
        if i:
            grid.append(f'<line class="gl" x1="{X0}" x2="{X1}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        grid.append(
            f'<text class="gt" x="{X0 - 12}" y="{y(v) + 4:.1f}" text-anchor="end">${v:.2f}</text>'
        )
    for n in range(1, N + 1):
        grid.append(f'<text class="gt" x="{x(n):.1f}" y="{Y0 + 24}" text-anchor="middle">{n}</text>')
    c["grid"] = "".join(grid)
    c["base"] = f'<line class="gb" x1="{X0}" x2="{X1}" y1="{Y0}" y2="{Y0}"/>'
    # Where switching overtakes staying (between requests 5 and 6) and the request-6 marker.
    cross = (SWITCH_FIRST - SWITCH_WARM) / (STAY - SWITCH_WARM)  # about 5.37
    c["pay-x"] = f"{x(cross):.1f}"
    c["pay-y"] = f"{y(STAY * cross):.1f}"
    c["data"] = (
        f'data-x0="{X0}" data-x1="{X1}" data-y0="{Y0}" data-ytop="{YTOP}" data-vmax="{VMAX}" '
        f'data-stay="{STAY}" data-first="{SWITCH_FIRST}" data-warm="{SWITCH_WARM}" data-alt="{ALTERNATE}"'
    )
    c["guide-x"] = f"{x(6):.1f}"
    c["stay-y"] = f"{y(stay(6)):.1f}"
    c["switch-y"] = f"{y(switch(6)):.1f}"
    return c


def svg_inline(raw: str, cls: str, ids: count) -> str:
    raw = re.sub(r'\s(width|height)="[^"]*"', "", raw, count=2)
    # Gradient ids must be unique on the page when a mark appears more than once.
    for old in set(re.findall(r'id="([^"]+)"', raw)):
        new = f"g{next(ids)}"
        raw = raw.replace(f'id="{old}"', f'id="{new}"').replace(f"#{old})", f"#{new})")
    return raw.replace("<svg ", f'<svg class="{cls}" aria-hidden="true" focusable="false" ', 1)


def sprite(names: list[str], ids: count) -> str:
    """Each brand mark once, as a <symbol>; gradients sit outside the symbols so <use> can see them."""
    defs, symbols = [], []
    for name in names:
        raw = svg_inline((HERE / "icons" / f"{name}.svg").read_text(encoding="utf-8"), "x", ids)
        vb = re.search(r'viewBox="([^"]+)"', raw).group(1)
        body = re.sub(r"^<svg[^>]*>|</svg>$", "", raw.strip())
        defs += re.findall(r"<defs>(.*?)</defs>", body, flags=re.S)
        body = re.sub(r"<defs>.*?</defs>", "", body, flags=re.S)
        symbols.append(f'<symbol id="l-{name}" viewBox="{vb}">{body}</symbol>')
    return ('<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">'
            f'<defs>{"".join(defs)}</defs>{"".join(symbols)}</svg>')


def main() -> None:
    page = (HERE / "index.src.html").read_text(encoding="utf-8")
    ids = count(1)
    geo = chart()

    def sub(m: re.Match) -> str:
        kind, name = m.group(1), m.group(2)
        if kind == "icon":
            raw = (HERE / "icons" / f"solar-{name}.svg").read_text(encoding="utf-8")
            return svg_inline(raw, "i", ids)
        if kind == "logo":
            used.add(name)
            vb = re.search(r'viewBox="([^"]+)"', (HERE / "icons" / f"{name}.svg").read_text(encoding="utf-8")).group(1)
            return f'<svg class="logo" viewBox="{vb}" aria-hidden="true" focusable="false"><use href="#l-{name}"/></svg>'
        if kind == "link":
            return LINKS[name]
        if name == "sprite":
            return "{{sprite}}"
        return geo[name]

    used: set[str] = set()
    page = re.sub(r"\{\{(icon|logo|chart|link):([a-z0-9-]+)\}\}", sub, page)
    page = page.replace("{{sprite}}", sprite(sorted(used), ids))
    assert "{{" not in page, "unresolved token"
    assert "{{sprite}}" not in page
    assert "—" not in page, "em dash in page"
    (SITE / "index.html").write_text(page, encoding="utf-8")
    print(f"wrote site/index.html ({len(page) // 1024} KB)")


if __name__ == "__main__":
    main()
