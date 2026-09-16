"""Is the dead Reactor video layer covering the still?

CDP screenshots composite the DOM stills but not the video element's surface,
which is why every screenshot in this session looked healthy while the screen
was black. So don't screenshot it -- interrogate the element.
"""
import sys

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

JS = """() => {
  const pick = (sel) => {
    const n = document.querySelector(sel);
    if (!n) return { sel, missing: true };
    const cs = getComputedStyle(n);
    const r = n.getBoundingClientRect();
    return {
      sel,
      cls: n.className,
      hidden: n.classList.contains('hidden'),
      z: cs.zIndex,
      op: cs.opacity,
      disp: cs.display,
      vis: cs.visibility,
      bgcolor: cs.backgroundColor,
      rect: [Math.round(r.width), Math.round(r.height)],
      covers: r.width > window.innerWidth * 0.9 && r.height > window.innerHeight * 0.9,
      hasStream: n.tagName === 'VIDEO' ? !!n.srcObject : null,
      readyState: n.tagName === 'VIDEO' ? n.readyState : null,
      paused: n.tagName === 'VIDEO' ? n.paused : null,
      vw: n.tagName === 'VIDEO' ? n.videoWidth : null,
      vh: n.tagName === 'VIDEO' ? n.videoHeight : null,
    };
  };
  return {
    body: document.body.className,
    video: pick('#reactor-video'),
    freeze: pick('#reactor-freeze'),
    mode: (window.Renderer && Renderer.mode) || '(none)',
    terminalStills: !!(window.Renderer && Renderer._terminalStills),
    lockedStills: !!(window.Renderer && Renderer.lockedStills),
    reactorActive: (() => {
      try { return !!(window.ReactorRenderer.isActive && window.ReactorRenderer.isActive()); }
      catch (e) { return 'threw'; }
    })(),
    reactorShowing: (() => {
      try { return !!(window.ReactorRenderer.isShowing && window.ReactorRenderer.isShowing()); }
      catch (e) { return 'threw'; }
    })(),
  };
}"""


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9333")
        pages = [p for p in browser.contexts[0].pages if "standalone" in p.url]
        if not pages:
            print("no app page open")
            return 1
        info = pages[0].evaluate(JS)

        print("renderer mode   :", info["mode"])
        print("terminalStills  :", info["terminalStills"])
        print("lockedStills    :", info["lockedStills"])
        print("reactor active  :", info["reactorActive"])
        print("reactor showing :", info["reactorShowing"])
        print("body class      :", info["body"])
        print()
        for key in ("video", "freeze"):
            d = info[key]
            if d.get("missing"):
                print("%s: ELEMENT MISSING" % d["sel"])
                continue
            print("%s" % d["sel"])
            print("   hidden=%s  z=%s  opacity=%s  display=%s  visibility=%s"
                  % (d["hidden"], d["z"], d["op"], d["disp"], d["vis"]))
            print("   bgcolor=%s  size=%s  covers_viewport=%s"
                  % (d["bgcolor"], d["rect"], d["covers"]))
            if d["hasStream"] is not None:
                print("   srcObject=%s readyState=%s paused=%s videoSize=%sx%s"
                      % (d["hasStream"], d["readyState"], d["paused"], d["vw"], d["vh"]))
            verdict = (not d["hidden"]) and d["covers"] and d["op"] not in ("0", 0)
            if verdict:
                print("   >>> THIS LAYER IS ON TOP OF THE STILL AND OPAQUE")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
