# Notifications and smart-home integration

Open **Notifications** in the manager sidebar. MQTT starts disabled; no broker or
smart-home installation is needed to run the miner or see dashboard link warnings.
Select global events and, for each account, destinations and events. Both selections
must allow an event for delivery. New accounts inherit all event selections.

## Telegram and Discord

Configure Telegram or Discord in each account's existing miner settings. The manager
uses those destinations for game-account linking required/restored, login required,
and session renewal failure alerts. Existing drop-claimed and new-release messages
continue through the miner's original notification settings; the manager does not
send a second copy of claimed-drop messages. MQTT also supports drop-claimed events.
WhatsApp is not a built-in destination. An external automation can forward MQTT
events to other services later.

Use the manager's WebUI password. Leave the embedded worker's separate native
dashboard password disabled: enabling it blocks the manager's internal observation
and notification bridge. Those worker ports are bound to container loopback.

An initially unlinked selected campaign produces one warning. A confirmed change
from linked to unlinked, or back again, produces another event. Observations are
saved encrypted so restarts do not repeat the same warning. Failed or incomplete
inventory requests count as unknown, never as proof of a lost link. The dashboard
keeps last-known warnings when a worker becomes unavailable.

This uses Twitch's campaign link status for selected games with unclaimed wanted
game rewards. It cannot verify a publisher's private account association or catch
an unlink that Twitch still reports as linked. Detection follows successful miner
inventory refreshes (normally 30 minutes), plus the manager's 30-second polling.
Completed/expired campaigns do not generate link alerts. Native alerts are attempted
once, with delivery failures shown in Notifications; there are no repeating reminders.

## Add MQTT later

1. Install a broker such as Mosquitto and create a dedicated publisher credential.
   openHAB connects to a broker; it does not supply one itself.
2. Enter its hostname/IP and port in Notifications. Use TLS with certificate
   verification for an encrypted connection, normally port 8883. Plain MQTT,
   normally port 1883, sends credentials and payloads without transport encryption.
3. Save and enable MQTT publishing. Broker credentials are encrypted with the
   existing account-data key. The password is never returned to the browser;
   leave its field blank to retain it, or select **Clear saved broker password**.
4. Check the connection status and **Send MQTT test**. Queued means accepted
   locally; the last-published time advances after broker acknowledgement. It does
   not prove that a downstream automation processed the event.

Use the displayed topic root, which includes a persistent random installation ID:

| Topic below `twitch-drops/INSTALLATION_ID` | Payload | Retained |
| --- | --- | --- |
| `availability` | `online` or `offline` | Yes |
| `ACCOUNT_ID/state` | Current account status and link warnings (JSON) | Yes |
| `ACCOUNT_ID/events` | One event (JSON) | No |
| `test` | Synthetic test event (JSON) | No |

The account ID is the manager's internal ID, not a Twitch ID. State includes the
account label, status, inventory freshness, link warnings and renewal error code.
Payloads contain selected game/campaign/reward names and public linking URLs when
available, never session proofs, cookies or broker credentials. Treat broker access
and retained messages as private account information.

Example event (fields vary by event):

```json
{
  "event": "account_link_required",
  "event_id": "unique-event-id",
  "timestamp": 1795400000,
  "account_id": "internal-account-id",
  "account": "Account One",
  "game": "Sample Game",
  "campaign": "Sample Campaign",
  "change": "lost",
  "link_url": "https://example.com/link"
}
```

Event names are `account_link_required`, `account_link_restored`, `drop_claimed`,
`login_required` and `renewal_failed`. The first claimed-drop history read establishes
a baseline; old claims are not replayed. Global/per-account event switches filter
events, while retained state still describes current account warnings. Disable an
account's MQTT destination to clear its retained state while connected.

Publishing uses QoS 1 and reconnects with backoff. Consumers should deduplicate
`event_id` because reconnects can repeat delivery. Up to 256 events wait in memory
for at most an hour; overflow discards the oldest. Restarting the app or changing
broker settings clears this queue. This is not a durable delivery service.
Availability uses a retained last will and 30-second heartbeat. Check account
freshness/status as well as installation availability before acting on state.

Deleting an account clears its retained state when the publisher can connect.
Broker records can outlive the app if it is stopped, settings change, or cleanup
cannot connect. In particular, restarting before a pending clear reaches the broker
loses that in-memory clear; clear obsolete retained topics at the broker if necessary.

## Connect a smart-home system

In [openHAB's MQTT binding](https://www.openhab.org/addons/bindings/mqtt/), create a
Broker Thing and Generic MQTT Thing. Use a String channel with the account's
`state` topic for state, and a trigger channel with its `events` topic for rules.
Parse JSON in the rule or with the JSONPATH transformation. Persist processed
event IDs if duplicate notifications would be a problem. Subscribe to
`twitch-drops/INSTALLATION_ID/+/events` to handle all accounts in one automation.

No MQTT subscriptions, remote commands or automatic device discovery are enabled
in this foundation. HomeKit does not consume MQTT directly; a separate bridge or
home-automation system would expose suitable notifications/devices to it. Configure
that integration when your home system is available; no extra miner ports are needed.
