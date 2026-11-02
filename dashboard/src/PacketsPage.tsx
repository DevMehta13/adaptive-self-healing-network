import { useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { axisTime } from './chartData';
import { FRAME_TYPES, typeLabel } from './frames';
import type { FrameRow } from './frames';
import { lateLabel, seconds } from './format';
import type { LiveState, TimeMap } from './liveData';
import { ackEvidence, frameFacts, matchesFilter, summarize } from './packetFacts';
import type { FrameFilter } from './packetFacts';
import {
  DELIVERY_ACK_PAYLOAD, DELIVERY_ACK_PAYLOAD_SIZE, HEADER_FIELDS, HEADER_SIZE, anatomy, bytesOf, cellOfByte, cellRange, headerMismatches,
} from './packets';
import type { Cell, CellKey } from './packets';
import { PageHeader } from './PageHeader';
import { LiveGate, LiveTag } from './pages';
import type { Live } from './useLive';

const cssKey = (key: CellKey) => key === 'payload_len' ? 'length' : key;
/** Rows shown at first (and added by "Show more"): fewer on a phone, where each row is a card. */
const PAGE = typeof window !== 'undefined' && window.matchMedia?.('(max-width: 700px)').matches ? 30 : 100;

export function PacketsPage({ live, data }: { live: Live; data: LiveState }) {
  return <>
    <PageHeader title="Packets"
      shows="The radio frames that node C handed up to the laptop: each one’s 14-byte header, its payload and the exact bytes."
      reading="Pick a frame in the log to see its bytes laid out field by field; hover or focus a field to find its bytes. Only what C hands up is here (see the scope box), so a missing frame is not proof that nothing was sent."
      aside={<LiveTag live={live} />} />
    <LiveGate live={live}>{view => data.timeMap
      ? <PacketsBody mode={view.mode} data={data} map={data.timeMap} />
      : null}</LiveGate>
  </>;
}

function Scope({ mode }: { mode: string }) {
  return <section className="scope-note" aria-labelledby="scope-title">
    <h2 id="scope-title">What you see here, and what you do not</h2>
    <ul>
      <li><strong>Seen:</strong> frames <em>handed up by C</em>: DATA that C accepts (new or repeated), and the TOPOLOGY_REPORT, ROUTE_INSTALL_ACK, LOCAL_FAILOVER_REPORT and TELEMETRY addressed to C.</li>
      <li><strong>Not seen:</strong> HELLO, PROBE, PROBE_REPLY and DELIVERY_ACK, frames the controller sends, and every hop between other boards. The laptop never receives them.</li>
      <li><strong>Decoded, not measured:</strong> version, type and length come from the bytes by the backend’s protocol codec.</li>
      {mode === 'simulation'
        ? <li className="sim-bytes"><strong>Simulation:</strong> these bytes come from the simulator’s codec, not from a radio.</li>
        : <li><strong>Hardware:</strong> the bytes are what node C received on the radio and copied to its serial line.</li>}
    </ul>
  </section>;
}

function PacketsBody({ mode, data, map }: { mode: string; data: LiveState; map: TimeMap }) {
  const [frozen, setFrozen] = useState<FrameRow[] | null>(null);
  const [filter, setFilter] = useState<FrameFilter>('all');
  const [selected, setSelected] = useState<FrameRow | null>(null);
  const [limit, setLimit] = useState(PAGE);
  const anatomyRef = useRef<HTMLElement>(null);
  const rows = frozen ?? data.frames;
  const summary = useMemo(() => summarize(rows), [rows]);
  const facts = useMemo(() => frameFacts(rows), [rows]);
  const acks = useMemo(() => ackEvidence(rows), [rows]);
  const matching = useMemo(() => rows.filter(row => matchesFilter(row, filter)).reverse(), [rows, filter]);

  const select = (row: FrameRow, scroll = false) => {
    setSelected(row);
    if (scroll) anatomyRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  };

  return <>
    <Scope mode={mode} />
    <Summary summary={summary} count={rows.length} paused={frozen !== null} />
    <section className="detail-panel anatomy-panel" aria-labelledby="anatomy-title" ref={anatomyRef}>
      <h3 id="anatomy-title">Anatomy of a frame</h3>
      <Anatomy row={selected} map={map} />
    </section>
    <section className="detail-panel frame-log" aria-labelledby="log-title">
      <div className="log-head">
        <h3 id="log-title">Frame log</h3>
        <div className="chart-controls">
          <label className="filter">Show <select value={filter} onChange={event => { setFilter(event.target.value as FrameFilter); setLimit(PAGE); }}>
            <option value="all">All types</option>
            {FRAME_TYPES.map(type => <option key={type} value={type}>{typeLabel(type)}</option>)}
            <option value="rejected">Rejected</option>
          </select></label>
          <div className="segmented" role="group" aria-label="Follow new frames">
            <button type="button" aria-pressed={frozen === null} onClick={() => setFrozen(null)}>Live</button>
            <button type="button" aria-pressed={frozen !== null} onClick={() => setFrozen(data.frames)}>Paused</button>
          </div>
        </div>
      </div>
      <p className="chart-caption">Newest first, in the order the controller learned of each frame. Time is when C received it ({mode === 'hardware' ? 'the laptop’s clock' : 'the simulated clock, mm:ss'}); a replayed frame sits at its time at C, not when it was handed up. {frozen ? 'Paused: the log, the summary and the facts are frozen; press Live to follow new frames. ' : ''}{rows.length} frames in this view (at most 1,000 are kept).</p>
      {matching.length ? <>
        <div className="table-scroll"><table className="frame-table">
          <thead><tr><th scope="col">Time</th><th scope="col">Type</th><th scope="col">src → dst</th><th scope="col">Session</th><th scope="col">Seq</th><th scope="col">TTL</th><th scope="col">Size</th><th scope="col">Via</th><th scope="col">Flags</th></tr></thead>
          <tbody>{matching.slice(0, limit).map(row => <LogRow key={row.key} row={row} map={map} selected={selected?.key === row.key} onSelect={() => select(row, true)} />)}</tbody>
        </table></div>
        {matching.length > limit && <p className="show-more"><button type="button" className="plain-button" onClick={() => setLimit(value => value + PAGE)}>Show {Math.min(PAGE, matching.length - limit)} more ({matching.length - limit} older)</button></p>}
      </> : <p className="empty-state">{rows.length
        ? 'No frame of this kind in the buffer. Choose another type.'
        : 'No frame has been handed up by C yet. Frames appear here, newest first, as soon as one arrives; the controller’s reports and telemetry come every few seconds.'}</p>}
    </section>
    <Facts facts={facts} map={map} onSelect={id => { const row = rows.find(r => r.id === id); if (row) select(row, true); }} />
    <Acknowledgements acks={acks} map={map} mode={mode} onSelect={row => select(row, true)} />
  </>;
}

function Summary({ summary, count, paused }: { summary: ReturnType<typeof summarize>; count: number; paused: boolean }) {
  const tile = (label: string, value: number, note?: string) => <div className="key-number"><span>{label}</span><strong>{value}</strong>{note && <small>{note}</small>}</div>;
  const widest = Math.max(1, ...summary.ttl.map(t => t.count));
  return <section className="summary-strip" aria-labelledby="summary-title">
    <h2 className="section-title" id="summary-title">Summary of the {count} frames in this view{paused ? ' (paused)' : ''}</h2>
    <div className="summary-tiles">
      {FRAME_TYPES.map(type => <div className="key-number" key={type}><span>{typeLabel(type)}</span><strong>{summary.byType[type]}</strong></div>)}
      {tile('Repeated readings', summary.duplicates, 'DATA that C had already seen')}
      {tile('Replayed', summary.replayed, 'held at C, handed up late')}
      {tile('Rejected', summary.rejected, 'refused by the codec')}
    </div>
    <div className="ttl-dist">
      <h3>TTL on arrival at C</h3>
      {summary.ttl.length ? <ul>{summary.ttl.map(t => <li key={t.ttl}>
        <span className="ttl-label">TTL {t.ttl}</span>
        <span className="ttl-bar" aria-hidden="true"><span style={{ width: `${(100 * t.count) / widest}%` }} /></span>
        <span className="ttl-count">{t.count}</span>
      </li>)}</ul> : <p className="empty-state">No decoded frame yet.</p>}
    </div>
  </section>;
}

function LogRow({ row, map, selected, onSelect }: { row: FrameRow; map: TimeMap; selected: boolean; onSelect: () => void }) {
  const h = row.header;
  return <tr className={`${selected ? 'selected ' : ''}${row.rejected ? 'rejected-row' : ''}`} onClick={onSelect}>
    <td data-label="Time"><button type="button" className="row-button" aria-pressed={selected} aria-label={`Frame ${row.id}, ${h ? h.type_name : 'rejected'}, at ${axisTime(map, row.at)}`}>{axisTime(map, row.at)}</button></td>
    <td data-label="Type">{h ? typeLabel(h.type_name) : 'Rejected'}</td>
    <td data-label="src → dst">{h ? `${h.src} → ${h.dst}` : '—'}</td>
    <td data-label="Session" className="num">{h ? h.session : '—'}</td>
    <td data-label="Seq" className="num">{h ? h.seq : '—'}</td>
    <td data-label="TTL" className="num">{h ? h.ttl : '—'}</td>
    <td data-label="Size" className="num">{row.size} B</td>
    <td data-label="Via">{row.via}</td>
    <td data-label="Flags" className="flags">
      {row.dup === true && <span className="flag dup">repeat (dup)</span>}
      {row.replayed && <span className="flag replayed">{lateLabel(row.delay_ms)}</span>}
      {row.rejected && <span className="flag rejected">rejected: {row.rejected.reason}</span>}
      {!row.dup && !row.replayed && !row.rejected && <span className="no-flag">—</span>}
    </td>
  </tr>;
}

function Anatomy({ row, map }: { row: FrameRow | null; map: TimeMap }) {
  const [hover, setHover] = useState<CellKey | null>(null);
  if (!row) {
    return <>
      <p className="chart-caption">Select a frame in the log below to fill these cells with its values. The layout is the radio header of docs/PROTOCOL.md: 14 bytes, then the payload.</p>
      <div className="anatomy-scroll" tabIndex={0} role="group" aria-label="Header layout, scrolls sideways on narrow screens">
        <ol className="anatomy-strip">
          {HEADER_FIELDS.map(field => <li key={field.key}><div className={`cell empty f-${cssKey(field.key)}`}>
            <span className="cell-label">{field.label}</span><span className="cell-bytes">—</span>
            <span className="cell-meta">byte{field.size > 1 ? 's' : ''} {field.offset}{field.size > 1 ? `–${field.offset + field.size - 1}` : ''}</span>
          </div></li>)}
          <li><div className="cell empty f-payload wide"><span className="cell-label">payload</span><span className="cell-bytes">—</span><span className="cell-meta">from byte {HEADER_SIZE}</span></div></li>
        </ol>
      </div>
    </>;
  }
  const cells = anatomy(row);
  const bytes = bytesOf(row.hex);
  const active = hover ? cells?.find(cell => cell.key === hover) ?? null : null;
  const [from, to] = hover ? cellRange(hover, bytes.length) : [0, 0];
  const mismatched = headerMismatches(row);
  const h = row.header;
  return <>
    <p className="selected-line"><strong>Frame {row.id}</strong> · {h ? typeLabel(h.type_name) : 'Rejected'} · {h ? `${h.src} → ${h.dst}` : 'not decoded'} · handed to C by <strong>{row.via}</strong> · {row.size} bytes · received {axisTime(map, row.at)}{row.replayed ? <> · <span className="flag replayed">{lateLabel(row.delay_ms)}</span></> : null}{row.dup === true ? <> · <span className="flag dup">repeat (dup)</span></> : null}</p>
    {row.rejected && <p className="state-banner stale" role="note"><strong>Refused by the codec: {row.rejected.reason}.</strong> Nothing is decoded from bytes the codec refused, so there are no fields to show; the exact bytes are below.</p>}
    {cells && <div className="anatomy-scroll" tabIndex={0} role="group" aria-label="Frame anatomy, scrolls sideways on narrow screens">
      <ol className="anatomy-strip">
        {cells.map(cell => <li key={cell.key}><CellButton cell={cell} on={hover === cell.key} setHover={setHover} /></li>)}
      </ol>
    </div>}
    <p className="field-help" aria-live="off">{active
      ? <><strong>{active.label}</strong> · {active.size} byte{active.size > 1 ? 's' : ''} at offset {active.offset}: <code>{active.hex.match(/../g)?.join(' ')}</code>{active.key === 'session' || active.key === 'seq' ? <> read little-endian (last byte first) = <strong>{active.value}</strong></> : active.key === 'payload' ? null : <> = <strong>{active.value}</strong></>}. {active.meaning}</>
      : 'Hover or focus a field to see its bytes, its value and what it means. session and seq are stored little-endian: the first byte in the hex is the lowest.'}</p>
    {mismatched.length > 0 && <p className="state-banner stale" role="alert"><strong>The bytes and the decoded header disagree</strong> for: {mismatched.join(', ')}. Trust the bytes below and report this.</p>}
    <div className="hexbox" tabIndex={0} role="group" aria-label="Raw bytes of the frame">
      <code>{bytes.map((byte, index) => {
        const key = cellOfByte(index);
        const on = hover !== null && index >= from && index < to;
        return <span key={index} className={`hb f-${cssKey(key)}${on ? ' on' : ''}${key === 'payload' && index === HEADER_SIZE ? ' first' : ''}`}>{byte}</span>;
      })}</code>
    </div>
    <p className="chart-caption">Raw hex, exactly as C received it (lower-case, one pair per byte). Colours match the cells above; the first {HEADER_SIZE} bytes are the header{bytes.length > HEADER_SIZE ? `, the other ${bytes.length - HEADER_SIZE} the payload` : ''}.</p>
  </>;
}

function CellButton({ cell, on, setHover }: { cell: Cell; on: boolean; setHover: (key: CellKey | null) => void }) {
  const bytes = cell.hex.match(/../g) ?? [];
  const shownBytes = bytes.length > 6 ? `${bytes.slice(0, 5).join(' ')} …` : bytes.join(' ');
  return <button type="button" className={`cell f-${cssKey(cell.key)}${cell.key === 'payload' ? ' wide' : ''}${on ? ' on' : ''}`} aria-pressed={on}
    onMouseEnter={() => setHover(cell.key)} onMouseLeave={() => setHover(null)} onFocus={() => setHover(cell.key)} onBlur={() => setHover(null)}
    onClick={() => setHover(on ? null : cell.key)}>
    <span className="cell-label">{cell.label}{cell.key === 'session' || cell.key === 'seq' ? <span className="le"> · little-endian</span> : null}</span>
    <span className="cell-bytes">{shownBytes || '—'}</span>
    <span className="cell-value">{cell.value}</span>
    <span className="cell-meta">{cell.size} B · offset {cell.offset}</span>
  </button>;
}

function Facts({ facts, map, onSelect }: { facts: ReturnType<typeof frameFacts>; map: TimeMap; onSelect: (id: number) => void }) {
  const link = (id: number) => <button type="button" className="link-button" onClick={() => onSelect(id)}>frame {id}</button>;
  const items: ReactNode[] = [];
  if (facts.arrival.length) {
    for (const a of facts.arrival) {
      const relays = a.transmissions - 1;
      items.push(<li key={`ttl${a.ttl}`}><strong>{a.count} DATA frame{a.count > 1 ? 's' : ''} arrived with TTL {a.ttl}</strong>: that is {a.transmissions} radio transmission{a.transmissions > 1 ? 's' : ''}
        {relays === 0 ? ', straight from the source' : `, so ${relays} relay${relays > 1 ? 's' : ''} in between`} (handed to C by {a.via.join(' and ')}). A node starts a data frame at TTL 8 and each transmission uses one.{relays > 1 ? ' C can name only the last neighbor, not the earlier relays.' : ''}</li>);
    }
  } else items.push(<li key="nottl">No DATA frame in the buffer yet, so there is no TTL to read.</li>);
  if (facts.reboots.length) {
    for (const r of facts.reboots) items.push(<li key={`rb${r.id}`}><strong>{r.src} restarted</strong>: its session changed from {r.from} to {r.to} at {axisTime(map, r.at)} ({link(r.id)}). A new session means a new boot; its sequence numbers start again.</li>);
  } else items.push(<li key="norb">No board changed its session in these frames, so none shows a restart.</li>);
  if (facts.duplicates.length) {
    for (const d of facts.duplicates) items.push(<li key={`dup${d.src}${d.session}${d.seq}`}><strong>Reading {d.src} / session {d.session} / seq {d.seq} reached C {d.ids.length} times</strong> ({d.ids.map((id, i) => <span key={id}>{i ? ', ' : ''}{link(id)}</span>)}); C’s filter flagged {d.flagged} as repeats. The packet identity is (src, session, seq), so it counts once as delivered, and C acknowledges each copy again.</li>);
  } else items.push(<li key="nodup">No reading reached C twice in these frames.</li>);
  if (facts.replayed.count) {
    const l = facts.replayed.longest;
    items.push(<li key="rep"><strong>{facts.replayed.count} replayed frame{facts.replayed.count > 1 ? 's' : ''}</strong>: C held them while the controller was unreachable, then handed them up late. The longest was held {seconds(facts.replayed.maxDelayMs)}{l ? <> ({link(l.id)}, received by C at {axisTime(map, l.at)}, handed up at {axisTime(map, l.reachedAt)})</> : null}. Their times above are the times at C, so they sit earlier than the frames around them in the log.</li>);
  } else items.push(<li key="norep">No replayed frame: everything here was handed up as it arrived.</li>);
  return <section className="detail-panel facts-panel" aria-labelledby="facts-title">
    <h3 id="facts-title">What these frames show</h3>
    <ul className="facts-list">{items}</ul>
    <p className="chart-caption">Each line is read from header fields and from the flags C reports (dup, replayed, age); nothing is guessed about the boards between.</p>
  </section>;
}

function Acknowledgements({ acks, map, mode, onSelect }: { acks: ReturnType<typeof ackEvidence>; map: TimeMap; mode: string; onSelect: (row: FrameRow) => void }) {
  return <section className="detail-panel ack-panel" aria-labelledby="ack-title">
    <h3 id="ack-title">Delivery acknowledgements</h3>
    <p className="ack-note">When C accepts a reading it creates a <strong>DELIVERY_ACK</strong> and sends it back to A over the radio. C does not hand these to the laptop, so <strong>this page cannot show one</strong>. What follows is the format, and then the indirect evidence.</p>
    <h4>The format <span className="tag">format, not observed by the laptop</span></h4>
    <div className="anatomy-scroll" tabIndex={0} role="group" aria-label="DELIVERY_ACK layout, scrolls sideways on narrow screens">
      <ol className="anatomy-strip static">
        {HEADER_FIELDS.map(field => <li key={field.key}><div className={`cell static f-${cssKey(field.key)}`}>
          <span className="cell-label">{field.label}</span>
          <span className="cell-value">{field.key === 'type' ? '0x02 DELIVERY_ACK' : field.key === 'src' ? 'C, the acknowledger' : field.key === 'dst' ? 'the reading’s origin' : field.key === 'payload_len' ? '9' : field.key === 'session' || field.key === 'seq' ? 'the acknowledger’s own' : field.key === 'version' ? '1' : 'hop budget'}</span>
          <span className="cell-meta">{field.size} B · offset {field.offset}</span>
        </div></li>)}
        {DELIVERY_ACK_PAYLOAD.map(part => <li key={part.label}><div className="cell static f-payload">
          <span className="cell-label">payload · {part.label}</span><span className="cell-value">{part.meaning}</span>
          <span className="cell-meta">{part.size} B · offset {HEADER_SIZE + part.offset}</span>
        </div></li>)}
      </ol>
    </div>
    <p className="chart-caption">14-byte header + {DELIVERY_ACK_PAYLOAD_SIZE}-byte payload = {HEADER_SIZE + DELIVERY_ACK_PAYLOAD_SIZE} bytes. The payload names the reading it confirms by origin, session and seq (docs/PROTOCOL.md). Its own src, session and seq are the acknowledger’s, because the acknowledgement is itself a numbered frame. It says the destination accepted the frame, nothing about what was done with it.</p>
    <h4>The indirect evidence: A’s acknowledgement counter</h4>
    <p className="ack-note">A counts the acknowledgements that reach it and reports the total in its TELEMETRY frames (about every 5 s). Those frames do reach the laptop. {mode === 'simulation' ? 'In simulation the counter comes from the simulated node A.' : 'The counter is A’s own.'} A rising counter means acknowledgements are getting back to A; it does not show any single one.</p>
    {acks.length ? <div className="table-scroll"><table className="frame-table ack-table">
      <thead><tr><th scope="col">Time at C</th><th scope="col">Created at A</th><th scope="col">Acknowledged at A</th><th scope="col">Since the previous</th><th scope="col">Frame</th></tr></thead>
      <tbody>{acks.map(a => <tr key={a.row.key}>
        <td data-label="Time at C">{axisTime(map, a.row.at)}{a.row.replayed ? <small> replayed</small> : null}</td>
        <td data-label="Created at A" className="num">{a.created}</td>
        <td data-label="Acknowledged at A" className="num">{a.acknowledged}</td>
        <td data-label="Since the previous" className="num">{a.change === null ? '—' : a.change >= 0 ? `+${a.change}` : a.change}</td>
        <td data-label="Frame"><button type="button" className="link-button" onClick={() => onSelect(a.row)}>Show frame {a.row.id}</button></td>
      </tr>)}</tbody></table></div>
      : <p className="empty-state">No TELEMETRY frame from A in the buffer yet. A sends one about every 5 s once it is running.</p>}
  </section>;
}
