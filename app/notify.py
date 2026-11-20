"""
Digest mail: events found during one check cycle are collected and sent together.

Each event has its own recipient list (the email list chosen for that PV/IOC/...). Events are
grouped by *recipient*: everybody gets exactly one mail per cycle that contains only the events
addressed to them, and recipients who are due the same set of events share one message.
A cycle with a single event for a recipient sends the unchanged individual mail.
"""
from collections import OrderedDict

# Events that mean "good news"; everything else counts as a fault.
_GOOD = ('RECOVERED', 'RECONNECTED', 'RESTARTED')


class Digest:
    def __init__(self, db, cfg, log):
        self.db, self.cfg, self.log = db, cfg, log
        self.events = []

    def add(self, source_type, source_id, source_name, event, subject, body, emails):
        self.events.append(dict(type=source_type, id=source_id, name=source_name, event=event,
                                subject=subject, body=body, emails=list(emails or [])))

    def flush(self):
        """Send the collected events (one mail per distinct recipient set) and log each event."""
        if not self.events:
            return
        from .email_utils import send_notification_email
        by_addr = OrderedDict()
        for i, e in enumerate(self.events):
            for a in dict.fromkeys(e['emails']):
                by_addr.setdefault(a, []).append(i)
        groups = OrderedDict()
        for addr, idx in by_addr.items():
            groups.setdefault(tuple(idx), []).append(addr)

        result = {i: (True, None) for i in range(len(self.events))}
        for i, e in enumerate(self.events):
            if not e['emails']:
                result[i] = (False, 'No recipients specified')
        for idx, addrs in groups.items():
            subject, body = compose([self.events[i] for i in idx])
            ok, err = send_notification_email(addrs, subject, body, self.cfg)
            if not ok:
                for i in idx:
                    result[i] = (False, err)
        for i, e in enumerate(self.events):
            ok, err = result[i]
            self.log(self.db, e['type'], e['id'], e['name'], e['event'], e['body'], e['emails'], ok, err)
        self.events = []


def compose(events):
    """(subject, body) for one recipient: the event itself if alone, otherwise a combined digest."""
    if len(events) == 1:
        return events[0]['subject'], events[0]['body']
    events = sorted(events, key=lambda e: e['event'] in _GOOD)       # faults first, stable otherwise
    faults = [e for e in events if e['event'] not in _GOOD]
    good = len(events) - len(faults)
    counts = OrderedDict()
    for e in faults:
        counts[e['event']] = counts.get(e['event'], 0) + 1
    parts = ', '.join(f'{n} {k}' for k, n in counts.items())
    if faults:
        subject = f'[WATCHDOG] {len(faults)} fault{"s" if len(faults) != 1 else ""}: {parts}'
        if good:
            subject += f' (+{good} recovered)'
    else:
        subject = f'[WATCHDOG] {good} recovered'
    lines = [f'EPICS PV Watchdog — {len(events)} events', '=' * 50, '']
    lines += [f'  {e["subject"]}' for e in events]
    for e in events:
        lines += ['', '-' * 50, e['body'].rstrip()]
    return subject[:200], '\n'.join(lines) + '\n'
