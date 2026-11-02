import React, { useEffect, useRef, useState } from "react";
type Row = Record<string, any>;
const ny = (s: string) =>
  new Intl.DateTimeFormat("en-GB", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(s));
const date = (s: string) =>
  new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(s));
export default function CandleChart({
  run,
  symbol,
  session,
  selected,
  paperOrders = [],
  mode = "simulation",
}: {
  run: Row;
  symbol: string;
  session: string;
  selected?: Row;
  paperOrders?: Row[];
  mode?: "simulation" | "paper";
}) {
  const canvas = useRef<HTMLCanvasElement>(null),
    dialog = useRef<HTMLDialogElement>(null),
    geo = useRef<any>(null),
    drag = useRef<any>(null);
  const [expanded, setExpanded] = useState(false),
    [levels, setLevels] = useState(true),
    [viewport, setViewport] = useState({ offset: 0, minutes: 125 }),
    [hover, setHover] = useState<number | null>(null),
    [size, setSize] = useState(0);
  const candles = run.candles.filter(
      (c: Row) => c.symbol === symbol && date(c.start) === session,
    ),
    opening =
      mode === "paper"
        ? undefined
        : run.opening_ranges.find(
            (r: Row) => r.symbol === symbol && r.session === session,
          );
  const decisions =
    mode === "paper"
      ? []
      : run.decisions.filter(
          (d: Row) =>
            d.symbol === symbol && date(d.signal.observed_at) === session,
        );
  const candidateTrade =
    mode === "simulation" &&
    selected?.symbol === symbol &&
    date(selected.signal.observed_at) === session
      ? selected.trade
      : decisions[0]?.trade;
  const trade = candidateTrade?.entry_at ? candidateTrade : undefined;
  const first = candles.length
      ? Date.parse(opening?.start || candles[0].start)
      : 0,
    last = candles.length
      ? Date.parse(candles.at(-1).start) + 60000
      : first + 60000,
    total = Math.max(1, (last - first) / 60000);
  useEffect(() => {
    setViewport({ offset: 0, minutes: 125 });
    setHover(null);
  }, [run.evaluation_id, symbol, session, mode]);
  useEffect(() => {
    if (expanded) dialog.current?.showModal();
  }, [expanded]);
  useEffect(() => {
    if (selected?.trade?.entry_at && first) {
      const offset = Math.max(
        0,
        (Date.parse(selected.trade.entry_at) - first) / 60000 - 20,
      );
      const finish = selected.trade.exit_at
        ? (Date.parse(selected.trade.exit_at) - first) / 60000 + 25
        : offset + 100;
      setViewport({
        offset,
        minutes: Math.min(total, Math.max(50, finish - offset)),
      });
    }
  }, [selected?.signal_id, first, total]);

  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const obs = new ResizeObserver(() => setSize((v) => v + 1));
    obs.observe(el);
    return () => obs.disconnect();
  }, [expanded]);
  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const listener = (e: WheelEvent) => {
      e.preventDefault();
      setViewport((v) => {
        const n = Math.max(
          15,
          Math.min(total, v.minutes * (e.deltaY > 0 ? 1.15 : 0.87)),
        );
        return {
          offset: Math.max(
            0,
            Math.min(total - n, v.offset + (v.minutes - n) / 2),
          ),
          minutes: n,
        };
      });
    };
    el.addEventListener("wheel", listener, { passive: false });
    return () => el.removeEventListener("wheel", listener);
  }, [expanded, total]);
  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const ctx = el.getContext("2d");
    if (!ctx) return;
    const { width: w, height: h } = el.getBoundingClientRect();
    if (!w || !h) return;
    const dpr = devicePixelRatio || 1;
    el.width = w * dpr;
    el.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!candles.length) return;
    const minutes = Math.min(viewport.minutes, total),
      offset = Math.max(0, Math.min(total - minutes, viewport.offset)),
      start = first + offset * 60000,
      end = start + minutes * 60000;
    const visible = candles.filter(
      (c: Row) => Date.parse(c.start) >= start && Date.parse(c.start) < end,
    );
    if (!visible.length) {
      ctx.fillStyle = "#9aacc4";
      ctx.fillText(
        "No candles in this interval — missing data stays a gap.",
        20,
        50,
      );
      return;
    }
    const overlays: [string, any, string][] = [];
    if (opening && levels)
      overlays.push(
        ["OR high", opening.high, "#a8bddc"],
        ["OR low", opening.low, "#a8bddc"],
      );
    if (trade && levels) {
      if (trade.entry_price != null)
        overlays.push(["Entry", trade.entry_price, "#c2cfe2"]);
      if (trade.stop != null) overlays.push(["Stop", trade.stop, "#e38b92"]);
      if (trade.target != null)
        overlays.push(["Target", trade.target, "#77c8a8"]);
    }
    const matchingPaper = paperOrders.filter(
      (o) =>
        o.symbol === symbol &&
        o.filled_at &&
        date(o.filled_at) === session &&
        Date.parse(o.filled_at) >= start &&
        Date.parse(o.filled_at) < end &&
        o.average_fill_price != null &&
        Number(o.filled_quantity) > 0,
    );
    const values = visible
        .flatMap((c: Row) => [Number(c.low), Number(c.high)])
        .concat(
          overlays.map((o) => Number(o[1])),
          matchingPaper.map((o) => Number(o.average_fill_price)),
        ),
      lo = Math.min(...values),
      hi = Math.max(...values),
      pad = Math.max(0.02, (hi - lo) * 0.13),
      left = 8,
      right = w - 66,
      top = 22,
      bottom = h - 36,
      priceBottom = bottom - 65;
    const x = (t: any) =>
        left + ((Date.parse(t) - start) / (end - start)) * (right - left),
      y = (p: any) =>
        priceBottom -
        ((Number(p) - lo + pad) / (hi - lo + 2 * pad)) * (priceBottom - top),
      step = (right - left) / minutes;
    geo.current = { offset, minutes, start, end, left, right, step };
    ctx.font = "10px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "#8b9ab1";
    for (let i = 0; i < 5; i++) {
      const p = lo + ((hi - lo) * i) / 4;
      ctx.fillText(p.toFixed(2), right + 10, y(p));
    }
    if (opening && levels) {
      const a = Math.max(left, x(opening.start)),
        b = Math.min(right, x(opening.available_at));
      if (b > a) {
        ctx.fillStyle = "#819dc514";
        ctx.fillRect(
          a,
          y(opening.high),
          b - a,
          y(opening.low) - y(opening.high),
        );
      }
    }
    const vol = Math.max(...visible.map((c: Row) => c.volume));
    visible.forEach((c: Row) => {
      const cx = x(c.start) + step / 2,
        bw = Math.max(0.8, Math.min(11, step * 0.63)),
        up = Number(c.close) >= Number(c.open);
      ctx.strokeStyle = up ? "#35bd88" : "#ec6b76";
      ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath();
      ctx.moveTo(cx, y(c.high));
      ctx.lineTo(cx, y(c.low));
      ctx.stroke();
      ctx.fillRect(
        cx - bw / 2,
        Math.min(y(c.open), y(c.close)),
        bw,
        Math.max(1, Math.abs(y(c.close) - y(c.open))),
      );
      ctx.fillStyle = up ? "#35bd8829" : "#ec6b7626";
      const vh = vol ? (c.volume / vol) * 43 : 0;
      ctx.fillRect(cx - bw / 2, bottom - vh, bw, vh);
    });
    let prev = -100;
    overlays
      .sort((a, b) => Number(b[1]) - Number(a[1]))
      .forEach(([name, p, col]) => {
        ctx.strokeStyle = col + "70";
        ctx.setLineDash([3, 5]);
        ctx.beginPath();
        ctx.moveTo(left, y(p));
        ctx.lineTo(right, y(p));
        ctx.stroke();
        ctx.setLineDash([]);
        const yy = Math.max(y(p) - 9, prev + 15);
        prev = yy;
        ctx.fillStyle = col;
        ctx.fillText(`${name} ${Number(p).toFixed(2)}`, left + 9, yy);
      });
    const mark = (stamp: any, price: any, label: string, col: string) => {
      if (!stamp || price == null) return;
      const xx = x(stamp) + step / 2;
      if (xx < left || xx > right) return;
      const yy = y(price);
      ctx.fillStyle = col;
      ctx.beginPath();
      ctx.arc(xx, yy, 3.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.textAlign = "center";
      ctx.fillText(label, xx, yy - 14);
      ctx.textAlign = "left";
    };
    decisions.forEach((d: Row) => {
      mark(d.trade.entry_at, d.trade.entry_price, "SIM BUY", "#69ddb0");
      mark(d.trade.exit_at, d.trade.exit_price, "SIM EXIT", "#d8e4f5");
    });
    paperOrders
      .filter(
        (o) =>
          o.symbol === symbol &&
          o.filled_at &&
          date(o.filled_at) === session &&
          Number(o.filled_quantity) > 0,
      )
      .forEach((o) =>
        mark(
          o.filled_at,
          o.average_fill_price,
          `PAPER ${o.side.toUpperCase()}`,
          o.side === "buy" ? "#8ad9be" : "#f1a0a7",
        ),
      );
    const labels = w < 500 ? 4 : 7;
    for (let i = 0; i < labels; i++) {
      ctx.textAlign = i === 0 ? "left" : i === labels - 1 ? "right" : "center";
      ctx.fillStyle = "#8193ac";
      ctx.fillText(
        ny(new Date(start + ((end - start) * i) / (labels - 1)).toISOString()),
        left + ((right - left) * i) / (labels - 1),
        h - 13,
      );
    }
    ctx.textAlign = "left";
    if (hover != null) {
      const nearest = visible.reduce((a: Row, b: Row) =>
        Math.abs(x(a.start) - hover) < Math.abs(x(b.start) - hover) ? a : b,
      );
      const xx = x(nearest.start) + step / 2;
      ctx.strokeStyle = "#8da5c36b";
      ctx.setLineDash([3, 4]);
      ctx.beginPath();
      ctx.moveTo(xx, top);
      ctx.lineTo(xx, bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#d0dced";
      ctx.fillText(
        `${ny(nearest.start)}  O ${nearest.open}  H ${nearest.high}  L ${nearest.low}  C ${nearest.close}`,
        left + 3,
        9,
      );
    }
  }, [
    run,
    symbol,
    session,
    selected,
    levels,
    viewport,
    hover,
    size,
    expanded,
    paperOrders,
  ]);
  function focus() {
    if (!trade?.entry_at) return;
    const begin = Math.max(
        0,
        (Date.parse(trade.entry_at) - first) / 60000 - 20,
      ),
      finish = trade.exit_at
        ? (Date.parse(trade.exit_at) - first) / 60000 + 25
        : begin + 100;
    setViewport({ offset: begin, minutes: Math.max(50, finish - begin) });
  }
  const content = (
    <>
      <div className="chart-heading">
        <strong>
          {symbol}
          <span className="instrument-name"> / {session}</span>
        </strong>
        <div className="chart-tools">
          <button onClick={() => setLevels(!levels)} aria-pressed={levels}>
            Levels
          </button>
          <button onClick={() => setViewport({ offset: 0, minutes: total })}>
            Full session
          </button>
          <button onClick={focus} disabled={!trade?.entry_at}>
            Focus trade
          </button>
          <button
            aria-label={expanded ? "Close expanded chart" : "Expand chart"}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? "×" : "⛶"}
          </button>
        </div>
      </div>
      <div className="canvas-wrap">
        <canvas
          ref={canvas}
          aria-label={`${symbol} actual source candles, ${session}. Missing minutes remain gaps.`}
          role="img"
          onPointerDown={(e) => {
            drag.current = { x: e.clientX, offset: geo.current?.offset || 0 };
            e.currentTarget.setPointerCapture(e.pointerId);
          }}
          onPointerMove={(e) => {
            if (drag.current && geo.current) {
              const g = geo.current;
              setViewport((v) => ({
                ...v,
                offset: Math.max(
                  0,
                  Math.min(
                    total - v.minutes,
                    drag.current.offset - (e.clientX - drag.current.x) / g.step,
                  ),
                ),
              }));
            } else
              setHover(
                e.clientX - e.currentTarget.getBoundingClientRect().left,
              );
          }}
          onPointerUp={() => (drag.current = null)}
          onPointerCancel={() => (drag.current = null)}
          onPointerLeave={() => {
            if (!drag.current) setHover(null);
          }}
        />
      </div>
      <div className="chart-bottom">
        <span>
          {run.import_context?.synthetic
            ? "Invented test prices"
            : "Source candles"}{" "}
          ·{" "}
          {mode === "paper"
            ? "Alpaca paper session · broker fill markers only"
            : "Historical backtest · simulated trades"}{" "}
          · 1 minute
        </span>
        <span>New York · USD</span>
      </div>
      <details className="candle-data">
        <summary>Candle data ({candles.length})</summary>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                {["NY time", "Open", "High", "Low", "Close", "Volume"].map(
                  (v) => (
                    <th key={v}>{v}</th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {candles.map((c: Row) => (
                <tr key={c.start}>
                  <td>{ny(c.start)}</td>
                  {["open", "high", "low", "close", "volume"].map((k) => (
                    <td key={k}>{c[k]}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </>
  );
  return (
    <>
      {!expanded ? (
        <section className="chart-card glass">{content}</section>
      ) : (
        <dialog
          className="chart-card glass chart-dialog"
          ref={dialog}
          onCancel={() => setExpanded(false)}
        >
          {content}
        </dialog>
      )}
    </>
  );
}
