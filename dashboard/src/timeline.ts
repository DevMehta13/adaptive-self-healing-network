import { ageText } from './format';
import { axisTime } from './chartData';
import type { ChangeEvent, TimeMap } from './liveData';

export type Kind = 'route' | 'node' | 'link' | 'gateway' | 'presenter';

export const kindLabels: Record<Kind, string> = { route: 'Route', node: 'Node', link: 'Link', gateway: 'Controller link', presenter: 'What we did' };

const KIND_BY_TYPE: Record<string, Kind> = {
  route_computed: 'route', route_installing: 'route', route_active: 'route', route_install_failed: 'route', no_available_route: 'route',
  no_route_confirmed: 'route', route_candidate: 'route', route_candidate_dropped: 'route', epoch_advanced: 'route', policy_changed: 'route',
  local_failover: 'route', link_held_down: 'link', link_state_changed: 'link',
  node_seen: 'node', node_lost_contact: 'node', node_regained_contact: 'node', node_restarted: 'node', node_stale: 'node',
  gateway_link_lost: 'gateway', gateway_link_restored: 'gateway', replay_started: 'gateway', replay_finished: 'gateway', uplink_loss: 'gateway', serial_port: 'gateway',
  environment: 'presenter', runtime: 'presenter', reset: 'presenter',
};

/** Intermediate steps that explain *how* the controller got somewhere, not *what* happened. Hidden by "Major changes only". */
const MINOR_TYPES = new Set(['route_installing', 'route_computed', 'route_candidate', 'route_candidate_dropped', 'epoch_advanced', 'link_held_down', 'node_seen', 'node_stale', 'no_route_confirmed']);

/**
 * Major: what a viewer would tell a colleague: what we did, failovers, confirmed routes, lost contact, link and controller-link changes.
 * Minor: route installation steps, the plan before it is confirmed, hold-downs, first sightings, expiring reports, and links merely going stale.
 * An event type this code does not know is major, so a new kind of event is never hidden.
 */
export function isMajor(type: string, data: Record<string, unknown>): boolean {
  if (MINOR_TYPES.has(type)) return false;
  if (type === 'link_state_changed') return data.new === 'up' || data.new === 'down' || data.new === 'one_way';
  return true;
}

export const eventKind = (type: string): Kind => KIND_BY_TYPE[type] ?? 'route';

const text = (value: unknown, fallback = '?') => typeof value === 'string' && value ? value : typeof value === 'number' ? String(value) : fallback;
const path = (value: unknown) => Array.isArray(value) && value.length ? value.map(n => text(n)).join(' → ') : '';
const secs = (ms: unknown) => typeof ms === 'number' ? `${(ms / 1000).toFixed(1)} s` : '';
const linkName = (value: unknown) => text(value).replace('-', '–');
const readable = (value: unknown) => text(value, 'unknown').replaceAll('_', ' ');

const TRIGGER_WORDS: Record<string, string> = {
  local_failover_report: 'after A’s local failover was reported', topology_changed: 'after the topology changed', initial: 'first route',
  node_restarted: 'after a node restarted', retry_after_failure: 'retrying after a failed install', policy_better_route: 'a better route won the hysteresis',
  epoch_advanced: 'after the epoch advanced',
};

/** Title and optional detail line, in the API's wording ("local failover", "lost contact", "No available route", ...). */
export function eventTitle(event: Pick<ChangeEvent, 'type' | 'data'>): { title: string; detail?: string } {
  const d = event.data;
  switch (event.type) {
    case 'environment': return { title: text(d.label, 'Presenter changed the simulation') };
    case 'runtime': return { title: d.change === 'paused' ? 'Simulation paused' : d.change === 'resumed' ? 'Simulation resumed' : `Simulation speed ×${text(d.speed)}` };
    case 'reset': return { title: 'Run reset to its scenario', detail: typeof d.scenario === 'string' ? d.scenario : undefined };
    case 'node_seen': return { title: `${text(d.node)} reported in` };
    case 'node_lost_contact': return { title: `${text(d.node)} lost contact`, detail: Array.isArray(d.observed_by) ? `Seen as silent by ${d.observed_by.join(', ')}${typeof d.silent_for_ms === 'number' ? ` for ${secs(d.silent_for_ms)}` : ''}. A timeout cannot prove it is powered off.` : undefined };
    case 'node_regained_contact': return { title: `${text(d.node)} regained contact`, detail: d.restarted === true ? 'It restarted (new session)' : undefined };
    case 'node_restarted': return { title: `${text(d.node)} restarted`, detail: 'Rebooted too fast to be declared lost' };
    case 'node_stale': return { title: `${text(d.node)} reports went stale`, detail: typeof d.reason === 'string' ? d.reason : undefined };
    case 'link_state_changed': return { title: `Link ${linkName(d.link)} is now ${readable(d.new)}`, detail: d.old ? `Was ${readable(d.old)}` : undefined };
    case 'link_held_down': return { title: `Link ${linkName(d.link)} held down`, detail: typeof d.hold_down_ms === 'number' ? `Not used for routes for ${secs(d.hold_down_ms)}` : undefined };
    case 'route_computed': return { title: `Controller planned route ${path(d.path) || 'unknown'}`, detail: TRIGGER_WORDS[text(d.trigger, '')] };
    case 'route_installing': return { title: `Installing route (${readable(d.phase)})` };
    case 'route_active': {
      const trigger = text(d.trigger, '');
      const title = trigger === 'local_failover_report' ? `Controller confirmed the failover: route ${path(d.path)}`
        : trigger === 'topology_changed' ? `Controller rerouted: ${path(d.path) || 'unknown'}` : `Controller confirmed route ${path(d.path) || 'unknown'}`;
      return { title, detail: typeof d.distribution_ms === 'number' ? `Every node acknowledged in ${secs(d.distribution_ms)}` : undefined };
    }
    case 'route_install_failed': return { title: `Route installation ${readable(d.state)}`, detail: typeof d.reason === 'string' ? d.reason : undefined };
    case 'local_failover': return {
      title: `Local failover: ${text(d.node)} switched ${d.from ? `from ${text(d.from)} ` : ''}${d.to ? `to ${text(d.to)}` : 'with no backup left'}`,
      detail: `${d.reason === 'send_failures' ? 'After repeated send failures' : d.reason === 'neighbor_lost' ? 'Neighbour lost' : readable(d.reason)}${d.replayed === true ? ' · reported late, held at C during an outage' : ''}`,
    };
    case 'no_available_route': return { title: 'No available route', detail: typeof d.reason === 'string' ? d.reason : undefined };
    case 'no_route_confirmed': return { title: 'The source confirmed it was told there is no route' };
    case 'route_candidate': return { title: `Better route seen: ${path(d.path)}`, detail: `${text(d.evaluations)} of ${text(d.needed)} evaluations` };
    case 'route_candidate_dropped': return { title: 'Better route dropped', detail: readable(d.reason) };
    case 'epoch_advanced': return { title: 'Route epoch advanced' };
    case 'policy_changed': return { title: `Routing policy changed to ${readable(d.new)}`, detail: d.old ? `Was ${readable(d.old)}` : undefined };
    case 'gateway_link_lost': return { title: 'Controller lost its link to C', detail: 'The network view is stale from here: nodes, links and the route are what the controller knew when C went quiet.' };
    case 'gateway_link_restored': return { title: 'Controller’s link to C restored', detail: typeof d.lost_for_ms === 'number' ? `Lost for ${secs(d.lost_for_ms)}` : undefined };
    case 'replay_started': return { title: 'C started replaying held reports', detail: `${text(d.entries)} entries${typeof d.outage_ms === 'number' ? ` after an outage of about ${secs(d.outage_ms)}` : ''}` };
    case 'replay_finished': return { title: 'C finished replaying held reports', detail: `${text(d.replayed)} entries handed up` };
    case 'uplink_loss': return { title: `${text(d.count)} message${d.count === 1 ? '' : 's'} from C will never arrive`, detail: d.reason === 'c_restarted' ? 'C restarted and its buffer is gone' : 'C no longer holds them' };
    case 'serial_port': return { title: `Serial port ${text(d.port)} ${readable(d.state)}`, detail: typeof d.detail === 'string' ? d.detail : undefined };
    default: return { title: readable(event.type) };
  }
}

export interface TimelineItem { key: string; kind: Kind; tag: string; title: string; detail?: string; time: string; ago: string; presenter: boolean; major: boolean }

/** Newest first. Times use the same clock as the chart: wall clock in hardware mode, simulated mm:ss in simulation mode. */
export function buildTimeline(changes: ChangeEvent[], nowAt: number | null, map: TimeMap): TimelineItem[] {
  return [...changes].sort((a, b) => b.at - a.at || b.id - a.id).map(change => {
    const kind = eventKind(change.type);
    const { title, detail } = eventTitle(change);
    return {
      key: change.key, kind, tag: kindLabels[kind], title, detail, presenter: kind === 'presenter', major: isMajor(change.type, change.data),
      time: axisTime(map, change.at), ago: nowAt === null ? '' : ageText(Math.max(0, nowAt - change.at)),
    };
  });
}

export type Filter = 'all' | 'controller' | 'presenter';

export const filterTimeline = (items: TimelineItem[], filter: Filter, majorOnly = false) =>
  items.filter(item => (!majorOnly || item.major) && (filter === 'all' || item.presenter === (filter === 'presenter')));

/** The newest major changes, for the Overview. Same titles and tags as the Network timeline. */
export const majorChanges = (changes: ChangeEvent[], nowAt: number | null, map: TimeMap, limit = 5) =>
  filterTimeline(buildTimeline(changes, nowAt, map), 'all', true).slice(0, limit);

export const PAGE_SIZE = 15;

/** How many rows to show: a first page, then a page more each time, never more than exist. */
export const visibleCount = (total: number, pages: number) => Math.min(total, PAGE_SIZE * Math.max(1, pages));
