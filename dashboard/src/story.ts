import type { RecordedEvent, RecordedRun } from './fixtures';
import { elapsedText } from './format';
import type { Kind, TimelineItem } from './timeline';

export interface StoryMoment {
  cursor: number;
  title: string;
  explanation: string;
  source: 'injected' | 'controller' | 'gateway' | 'reading' | 'checkpoint';
}

/** Curated teaching stops; the numbered stream remains the source of state. */
export function storyMoments(run: RecordedRun): StoryMoment[] {
  const find = (type: string, match: (event: RecordedEvent) => boolean = () => true): number => {
    const id = run.events.find(event => event.type === type && match(event))?.event_id;
    if (id === undefined) throw new Error(`Recorded run is missing ${type}`);
    return id;
  };
  const stopB = (event: RecordedEvent) => event.data.action === 'stop_node' && event.data.target === 'B';
  const routeVia = (relay: string) => (event: RecordedEvent) => Array.isArray(event.data.path) && event.data.path[1] === relay;
  const moment = (cursor: number, title: string, explanation: string, source: StoryMoment['source']): StoryMoment => ({ cursor, title, explanation, source });
  const start = moment(0, 'Before reports arrive', 'The controller has not heard from any board yet, so it cannot confirm a route or a delivery.', 'checkpoint');
  const normal = moment(find('route_active', routeVia('B')), 'Route through B confirmed', 'The controller installed and confirmed A → B → C. D is available as a backup.', 'controller');
  const firstReading = moment(find('reading_received'), 'First reading reaches C', 'C received a reading through B. This is delivery evidence, separate from the planned route.', 'reading');
  const finish = moment(Math.max(...run.events.map(event => event.event_id ?? 0)), 'End of recorded run', 'The recording ends here. Check the latest received reading and the controller’s final route above.', 'checkpoint');

  if (run.name === 'Relay failure') {
    const backToB = run.events.filter(event => event.type === 'route_active' && routeVia('B')(event)).at(-1)?.event_id;
    if (backToB === undefined) throw new Error('Recorded run is missing return route');
    return [start, normal, firstReading,
      moment(find('environment', stopB), 'B is stopped', 'The presenter stops B in the simulation. The controller has not detected the failure yet.', 'injected'),
      moment(find('local_failover'), 'A switches to D', 'A uses its installed backup route before the controller confirms a new route.', 'controller'),
      moment(find('route_active', routeVia('D')), 'Route through D confirmed', 'The controller installs and confirms A → D → C.', 'controller'),
      moment(find('node_lost_contact'), 'B is marked out of contact', 'Neighbor reports show B is no longer answering. This does not prove why.', 'controller'),
      moment(find('environment', event => event.data.action === 'start_node' && event.data.target === 'B'), 'B is started again', 'The presenter restarts B in the simulation. The controller still has to hear from it.', 'injected'),
      moment(backToB, 'Route through B returns', 'The controller selects and confirms B again after its links recover.', 'controller'),
      finish];
  }

  return [start, normal, firstReading,
    moment(find('environment', event => event.data.action === 'gateway' && (event.data.detail as { attached?: boolean } | undefined)?.attached === false), 'C’s laptop link is disconnected', 'The presenter detaches the simulated gateway. The boards may continue, but the laptop stops receiving updates.', 'injected'),
    moment(find('gateway_link_lost'), 'Controller notices C is silent', 'The laptop marks its network view stale because C has stopped reporting.', 'controller'),
    moment(find('environment', stopB), 'B is stopped during the outage', 'The presenter stops B while the laptop cannot see the network.', 'injected'),
    moment(run.mid.last_event_id, 'Network view is stale', 'The controller’s last counts and route are old. The boards may be doing something different.', 'checkpoint'),
    moment(find('environment', event => event.data.action === 'gateway' && (event.data.detail as { attached?: boolean } | undefined)?.attached === true), 'C’s laptop link returns', 'The presenter reconnects the simulated gateway. C can now send held messages.', 'injected'),
    moment(find('replay_started'), 'C begins replaying', 'C sends the messages it held during the outage; their original times are earlier.', 'gateway'),
    moment(find('reading_received', event => event.data.replayed === true), 'First delayed reading arrives', 'This reading reached C earlier but only now reached the laptop. Its delay is shown below.', 'reading'),
    moment(find('local_failover'), 'A’s switch to D is reported', 'The controller learns late that A used its backup while the laptop link was down.', 'controller'),
    moment(find('route_active', routeVia('D')), 'Route through D confirmed', 'The controller catches up and confirms A → D → C.', 'controller'),
    finish];
}

export interface StoryProgress { position: number; total: number; label: string; percent: number }

/** Where the viewer is in the story: "Moment 3 of 10", for the heading and the progress bar. `step` counts from 0. */
export function storyProgress(step: number, total: number): StoryProgress {
  const position = Math.min(Math.max(step, 0), Math.max(total - 1, 0)) + 1;
  return { position, total, label: `Moment ${position} of ${total}`, percent: total <= 1 ? 100 : Math.round(((position - 1) / (total - 1)) * 100) };
}

const TAGS: Record<StoryMoment['source'], { kind: Kind; tag: string }> = {
  injected: { kind: 'presenter', tag: 'What we did' },
  controller: { kind: 'route', tag: 'Controller' },
  gateway: { kind: 'gateway', tag: 'Controller link' },
  reading: { kind: 'node', tag: 'Reading' },
  checkpoint: { kind: 'link', tag: 'Checkpoint' },
};

/** The story so far, as the same rows the live timeline uses: oldest first, presenter actions marked. `clockMs` gives each moment's recorded time. */
export function storyRows(moments: StoryMoment[], upTo: number, clockMs: (moment: StoryMoment) => number): TimelineItem[] {
  return moments.slice(0, upTo + 1).map(moment => {
    const { kind, tag } = TAGS[moment.source];
    return { key: `${moment.cursor}-${moment.title}`, kind, tag, title: moment.title, detail: moment.explanation, time: elapsedText(clockMs(moment)), ago: '', presenter: moment.source === 'injected', major: true };
  });
}
