"""Reproducible descriptive performance, never a trading or recovery authority.

Inputs are engine-produced dashboard runs. API callers retrieve them through the
integrity-checked archive; the CLI recomputes from canonical source bars.
"""
from datetime import datetime
from decimal import Decimal as D, localcontext
from random import Random

from orb.comparison import _daily, _metrics
from orb.evaluation import _digest
from orb.research_settings import canonical
from orb.session import exchange_session
from orb.strategy import NEW_YORK


def block_interval(values, *, block_length=5, resamples=2000, seed=1729):
    """Percentile circular moving-block interval for mean historical session P&L.

    Resample sessions (including zero-trade sessions), never individual trades.
    Historical dollars are resampled without rerunning adaptive account sizing.
    """
    n = len(values)
    if (type(block_length) is not int or not 1 <= block_length <= 20
            or type(resamples) is not int or not 200 <= resamples <= 5000
            or type(seed) is not int or not 0 <= seed < 2**32
            or n > 2000 or n * resamples > 4_000_000):
        raise ValueError('unsupported or excessive bootstrap request')
    result = dict(method='circular_moving_block_percentile', unit='NY_session_net_pnl_usd',
                  block_length=block_length, resamples=resamples, seed=seed,
                  confidence='0.95', session_count=n, lower=None, upper=None,
                  reason=None, interpretation='Descriptive, conditional on observed sessions and fixed historical sizing; not a probability of future profit or a multiple-testing adjustment.')
    if n < max(20, 4 * block_length):
        return dict(result, reason='insufficient_sessions_minimum_20_and_four_blocks')
    with localcontext() as ctx:
        ctx.prec = 80
        data = [D(v) for v in values]
        if any(not v.is_finite() for v in data): raise ValueError('nonfinite session P&L')
        rng = Random(seed)
        whole, remainder = divmod(n, block_length)
        sums = [sum((data[(i+j) % n] for j in range(block_length)), D(0)) for i in range(n)]
        tails = [sum((data[(i+j) % n] for j in range(remainder)), D(0)) for i in range(n)]
        samples = []
        for _ in range(resamples):
            total = sum((sums[rng.randrange(n)] for _ in range(whole)), D(0))
            if remainder: total += tails[rng.randrange(n)]
            samples.append(total / n)
        samples.sort()
        # Explicit linear interpolation, independent of numpy versions.
        def quantile(p):
            index = D(resamples-1) * p
            lo = int(index); weight = index-lo
            return canonical(samples[lo] * (1-weight) + samples[min(lo+1, resamples-1)] * weight)
        return dict(result, lower=quantile(D('.025')), upper=quantile(D('.975')))


def _benchmarks(run):
    """Independent full-capital, whole-share price-only buy-and-hold accounts."""
    e = run['evidence']; config = e['manifest']['config']
    out = []
    with localcontext() as ctx:
        ctx.prec = 80
        capital, bps, fee = (D(config[k]) for k in ('initial_cash', 'slippage_bps', 'fee_per_share'))
        bps = D(config.get('effective_market_side_bps', bps))
        for symbol in config['symbols']:
            candles = [c for c in run['candles'] if c['symbol'] == symbol]
            # Regular-session boundaries only; never substitute pre/postmarket bars.
            regular = []
            for c in candles:
                at = datetime.fromisoformat(c['start']); session = exchange_session(at.astimezone(NEW_YORK).date())
                if session and session.opens_at <= at < session.closes_at: regular.append(c)
            row = dict(symbol=symbol, net_price_pnl_usd=None, return_fraction=None, quantity=None)
            if run['metrics_eligible'] and regular:
                entry = D(regular[0]['open']) * (1 + bps/10000)
                exit = D(regular[-1]['close']) * (1 - bps/10000)
                quantity = int(capital / (entry+fee))
                pnl = quantity * (exit-entry-2*fee)
                row.update(net_price_pnl_usd=canonical(pnl), return_fraction=canonical(pnl/capital),
                           quantity=quantity, entry_at=regular[0]['start'], last_bar_at=regular[-1]['start'],
                           modelled_fees_usd=canonical(2*fee*quantity))
            out.append(row)
    return dict(cash_return_fraction='0', cash_assumptions='Zero interest and costs.',
                passive=out, assumptions='Separate fully allocated account per symbol; first regular open to last regular close; same per-side slippage/fee assumptions (including half-spread when declared), whole shares, uninvested cash earns zero. No ORB entry delay is applied to passive open-to-close holding. Price-only: no dividends, financing, settlement or corporate-action verification. Different exposure from ORB; not a combined portfolio or risk-matched comparison.')


def historical_performance(run):
    e = run['evidence']; m = e['manifest']; eligible = run['metrics_eligible']
    daily = _daily(run)
    trades = [t for t in e['portfolio']['trades'] if t['status'] == 'closed']
    with localcontext() as ctx:
        ctx.prec = 80
        pnls = [D(t['net_pnl']) for t in trades]
        gains = sum((p for p in pnls if p > 0), D(0))
        losses = -sum((p for p in pnls if p < 0), D(0))
        mean = sum(pnls, D(0))/len(pnls) if pnls else None
        stats = dict(closed_trade_count=len(trades),
                     net_pnl_usd=canonical(sum(pnls, D(0))) if eligible else None,
                     expectancy_usd=canonical(mean) if eligible and mean is not None else None,
                     win_fraction=canonical(D(sum(p > 0 for p in pnls))/len(pnls)) if eligible and pnls else None,
                     profit_factor=canonical(gains/losses) if eligible and losses else None,
                     profit_factor_reason='incomplete_evidence' if not eligible else 'no_losing_trades' if not losses else None)
    interval = block_interval([v['net_pnl_usd'] for v in daily.values()]) if eligible else dict(
        lower=None, upper=None, reason='incomplete_evidence')
    result = dict(schema_version='orb-performance-v1', evaluation_id=run['evaluation_id'],
        metrics_eligible=eligible, data_label=m['dataset']['label'], dataset=m['dataset'],
        engine=m['engine'], config=m['config'], applied_settings=run['applied_settings'],
        synthetic=bool(run.get('import_context', {}).get('synthetic')),
        holdout_status='retrospective_unsealed', session_count=len(daily), daily=daily,
        trade_statistics=stats, engine_metrics=_metrics(run), session_mean_interval=interval,
        benchmarks=_benchmarks(run), profitability_established=False,
        limitations=[
            'Retrospective descriptive report; positive historical results do not establish future profitability.',
            'Research baseline can include SPY and QQQ; the current paper worker trades SPY only. This is not a paper-equivalent strategy validation.',
            'Fees/slippage are frozen assumptions, not verified broker costs. Intrabar execution remains idealised.',
            'Block interval assumes local dependence is captured by five-session blocks; market regime shifts and strategy selection are not accounted for.',
            'Missing sessions or uncertain outcomes withhold aggregate results; no-trade complete sessions remain in the resample.',
            'No parameter optimisation, ranking, sealed holdout or forward observation was performed by this report.',
        ])
    result['report_id'] = _digest(result)
    return result


def render_performance(r):
    stats = r['trade_statistics']; metrics = r['engine_metrics']; interval = r['session_mean_interval']
    show = lambda v: 'Unavailable' if v is None else str(v)
    lines = ['# ORB performance report', '', f"Report: {r['report_id']}",
             f"Evaluation: {r['evaluation_id']}", f"Data: {r['data_label']}",
             f"Synthetic: {r['synthetic']}; eligible evidence: {r['metrics_eligible']}",
             f"Holdout: {r['holdout_status']}; profitability established: False", '',
             '| Measure | Value |', '| --- | ---: |',
             f"| Sessions (including no-trade sessions) | {r['session_count']} |",
             f"| Closed trades | {stats['closed_trade_count']} |",
             f"| Modelled net P&L (USD) | {show(stats['net_pnl_usd'])} |",
             f"| Expectancy per closed trade (USD) | {show(stats['expectancy_usd'])} |",
             f"| Win fraction | {show(stats['win_fraction'])} |",
             f"| Profit factor | {show(stats['profit_factor'])} |",
             f"| Return fraction | {show(metrics['total_return_fraction'])} |",
             f"| Sampled max drawdown fraction | {show(metrics['sampled_max_drawdown_fraction'])} |", '',
             '## Session uncertainty', '',
             f"95% descriptive interval for mean session net P&L (USD): {show(interval['lower'])} to {show(interval['upper'])}.",
             f"Unavailable reason: {interval.get('reason') or 'none'}.",
             'Circular five-session blocks; 2,000 resamples; seed 1729. Historical sizing is fixed.', '',
             '## Cost assumptions', '',
             f"Slippage: {r['config']['slippage_bps']} bps per market side; fee: {r['config']['fee_per_share']} USD/share/side.", '',
             '## Benchmarks', '', r['benchmarks']['assumptions'], '',
             '| Independent account | Price-only return fraction |', '| --- | ---: |', '| Cash, zero interest | 0 |']
    lines += [f"| {b['symbol']} | {show(b['return_fraction'])} |" for b in r['benchmarks']['passive']]
    if 'fill_assumptions' in r['config']:
        a = r['config']['fill_assumptions']
        lines += ['', f"ORB delayed-open entry: {a['entry_delay_minutes']} minutes; assumed full spread: {a['spread_bps']} bps.",
                  f"Effective cost per market side: {r['config']['effective_market_side_bps']} bps; limit targets unchanged.",
                  'No order/reservation while waiting. These are sensitivity assumptions, not measured quotes or broker fills.']
    lines += ['', '## Limitations', ''] + ['- '+v for v in r['limitations']]
    return '\n'.join(lines)+'\n'
