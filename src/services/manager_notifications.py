"""Narrow manager bridge: sanitized observations and existing notification channels."""
from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone
from urllib.parse import urlsplit

from src.services.discord_service import DiscordNotifier
from src.services.telegram_service import TelegramNotifier


class ManagerNotifications:
    EVENTS = {'account_link_required', 'account_link_restored', 'login_required', 'renewal_failed'}

    @staticmethod
    def public_link(value):
        if not isinstance(value, str) or len(value) > 2048:
            return None
        try:
            url = urlsplit(value)
            _ = url.port  # Validate malformed/non-numeric ports before exposing a link.
            if (url.scheme == 'https' and url.hostname and not url.username and not url.password
                    and not url.query and not url.fragment and not any(ord(c) < 32 for c in value)):
                return value
        except ValueError:
            pass
        return None

    @classmethod
    def snapshot(cls, client):
        logged_in = client._auth_state._logged_in.is_set()
        session = client.session_controller.status()
        observed = getattr(client, 'manager_inventory_at', None)
        ready = logged_in and observed is not None and not getattr(client, 'manager_inventory_fetching', False)
        campaigns = []
        claims = [{'id': entry['id'], 'game': entry['game'][:256],
                   'campaign': entry['campaign'][:256], 'reward': entry['drop_name'][:256]}
                  for entry in client.drop_history.get_entries(limit=2000)] if logged_in else []
        selected = {name.casefold() for name in client.settings.games_to_watch}
        now = datetime.now(timezone.utc)
        for campaign in client.inventory if ready else ():
            if campaign.game.name.casefold() not in selected:
                continue
            if campaign.ends_at <= now:
                continue
            wanted = any(drop.ends_at > now and drop.has_wanted_unclaimed_benefits(client.settings.mining_benefits)
                         and any(not benefit.type.is_badge_or_emote() for benefit in drop.benefits)
                         for drop in campaign.watch_drops)
            if wanted:
                campaigns.append({'id': campaign.id, 'game': campaign.game.name[:256],
                                  'campaign': campaign.name[:256], 'linked': campaign.linked,
                                  'link_url': cls.public_link(campaign.link_url)})
        return {'logged_in': logged_in, 'renewal_error': session.get('renewal_error'),
                'inventory_ready': ready, 'observed_at': observed,
                'refresh_minutes': client.settings.minimum_refresh_interval_minutes,
                'channels': {'telegram': bool(client.settings.telegram_bot_token and client.settings.telegram_chat_id),
                             'discord': DiscordNotifier.is_valid_webhook(client.settings.discord_webhook_url)},
                'campaigns': campaigns[:500], 'claims': claims}

    @classmethod
    async def send(cls, client, data):
        if not isinstance(data, dict) or set(data) != {'account', 'telegram', 'discord', 'events'}:
            raise ValueError('Invalid notification')
        if (not isinstance(data['account'], str) or len(data['account']) > 80
                or any(type(data[k]) is not bool for k in ('telegram', 'discord'))
                or not isinstance(data['events'], list) or not 1 <= len(data['events']) <= 10):
            raise ValueError('Invalid notification')
        lines = ['Account: ' + data['account']]
        for event in data['events']:
            if not isinstance(event, dict) or event.get('event') not in cls.EVENTS:
                raise ValueError('Invalid event')
            title = event['event'].replace('_', ' ').capitalize()
            parts = []
            for key in ('game', 'campaign'):
                value = event.get(key, '')
                if not isinstance(value, str) or len(value) > 256:
                    raise ValueError('Invalid event detail')
                if value:
                    parts.append(value)
            lines.append(title + (': ' + ' / '.join(parts) if parts else ''))
            link = cls.public_link(event.get('link_url'))
            if link:
                lines.append(link)
        message = '\n'.join(lines)[:3500]
        settings = client.settings
        async def telegram():
            if not data['telegram']:
                return False
            return await TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)._send_message(html.escape(message))
        async def discord():
            if not data['discord']:
                return False
            return await DiscordNotifier(settings.discord_webhook_url)._send_embed({
                'title': 'Twitch Drops account notification',
                'description': message, 'color': 0x9146FF})
        results = await asyncio.gather(telegram(), discord(), return_exceptions=True)
        return {'telegram': results[0] is True, 'discord': results[1] is True}
