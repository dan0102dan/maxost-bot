"""Durable album assembly, delivery checkpoints and owner-scoped content links."""
from __future__ import annotations

from .content import combine_album


class ContentStore:
    async def saved_step(self, job, step):
        row = await self.pool.fetchval('SELECT result FROM delivery_steps WHERE job_id=$1 AND owner=$2 AND step=$3', job['id'], job['owner'], step)
        return self.vault.open(job['owner'], f"step:{job['id']}:{step}", bytes(row)) if row is not None else None

    async def save_step(self, job, step, result):
        await self.pool.execute('INSERT INTO delivery_steps(job_id,owner,step,result) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING', job['id'], job['owner'], step, self.vault.seal(job['owner'], f"step:{job['id']}:{step}", result))

    async def save_links(self, job, pairs):
        origin = 'max' if job['direction'] == 'tg' else 'tg'
        async with self.pool.acquire() as c, c.transaction():
            for p in pairs:
                await c.execute('''INSERT INTO message_links(job_id,owner,dialog_id,tg_message_id,max_message_id,kind,origin,part,source_key,album_id,media_tag)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    ON CONFLICT(dialog_id,origin,source_key,part,tg_message_id) DO UPDATE SET
                        job_id=excluded.job_id,max_message_id=excluded.max_message_id,kind=excluded.kind,album_id=excluded.album_id,media_tag=excluded.media_tag''',
                    job['id'], job['owner'], job['dialog_id'], int(p['tg']), str(p['max']), p['kind'], origin,
                    p['part'] + job['part'], job['source_id'], p.get('album'), p.get('media_tag'))

    async def source_links(self, dialog, origin, source_key):
        return await self.pool.fetch('SELECT * FROM message_links WHERE owner=$1 AND dialog_id=$2 AND origin=$3 AND source_key=$4 ORDER BY part,id', dialog['owner'], dialog['id'], origin, str(source_key))

    async def remove_link(self, dialog, link_id):
        await self.pool.execute('DELETE FROM message_links WHERE id=$1 AND owner=$2 AND dialog_id=$3', link_id, dialog['owner'], dialog['id'])

    async def collect_album(self, dialog, group_id, message_id, payload, version):
        purpose = f"album:{dialog['id']}:{group_id}"
        async with self.pool.acquire() as c, c.transaction():
            # This lock also serializes a first member with concurrent first members.
            await c.fetchval('SELECT id FROM dialogs WHERE id=$1 AND owner=$2 FOR UPDATE', dialog['id'], dialog['owner'])
            row = await c.fetchrow('SELECT * FROM albums WHERE dialog_id=$1 AND owner=$2 AND group_id=$3 FOR UPDATE', dialog['id'], dialog['owner'], group_id)
            members = self.vault.open(dialog['owner'], purpose, bytes(row['body'])) if row else {}
            key = str(message_id)
            if key in members and members[key]['version'] >= version:
                return
            members[key] = {'version': version, 'payload': payload}
            combine_album([m['payload'] for m in members.values()])  # validate before acknowledging update
            await c.execute('''INSERT INTO albums(dialog_id,owner,group_id,body,ready_at)
                VALUES($1,$2,$3,$4,now()+interval '1.5 seconds') ON CONFLICT(dialog_id,group_id)
                DO UPDATE SET body=excluded.body,generation=albums.generation+1,
                    ready_at=excluded.ready_at,updated_at=now()''', dialog['id'], dialog['owner'], group_id,
                self.vault.seal(dialog['owner'], purpose, members))

    async def flush_albums(self):
        async with self.pool.acquire() as c, c.transaction():
            rows = await c.fetch('''SELECT * FROM albums WHERE generation>flushed_generation AND ready_at<=now()
                ORDER BY ready_at LIMIT 20''')
            for candidate in rows:
                # Match collect_album's lock order: dialog first, then album.
                locked = await c.fetchval('SELECT id FROM dialogs WHERE id=$1 AND owner=$2 FOR UPDATE SKIP LOCKED', candidate['dialog_id'], candidate['owner'])
                if locked is None:
                    continue
                row = await c.fetchrow('SELECT * FROM albums WHERE dialog_id=$1 AND owner=$2 AND group_id=$3 AND generation>flushed_generation AND ready_at<=now() FOR UPDATE', candidate['dialog_id'], candidate['owner'], candidate['group_id'])
                if row is None:
                    continue
                members = self.vault.open(row['owner'], f"album:{row['dialog_id']}:{row['group_id']}", bytes(row['body']))
                payload = combine_album([m['payload'] for m in members.values()])
                action = 'send' if not row['flushed_generation'] else 'edit'
                source = 'album:' + row['group_id']
                revision = '' if action == 'send' else str(row['generation'])
                body = payload if action == 'send' else {'parts': [payload]}
                purpose = f"job:{row['dialog_id']}:max:{action}:{source}:{revision}:0"
                await c.execute('''INSERT INTO jobs(owner,dialog_id,direction,action,source_id,revision,part,payload)
                    VALUES($1,$2,'max',$3,$4,$5,0,$6) ON CONFLICT DO NOTHING''', row['owner'], row['dialog_id'], action, source, revision, self.vault.seal(row['owner'], purpose, body))
                await c.execute('UPDATE albums SET flushed_generation=generation WHERE dialog_id=$1 AND group_id=$2', row['dialog_id'], row['group_id'])

    def card_data(self, row):
        return self.vault.open(row['owner'], f"card:{row['dialog_id']}:{row['kind']}:{row['max_message_id']}", bytes(row['data']))

    async def put_card(self, dialog, message_id, kind, data):
        return await self.pool.fetchrow('''INSERT INTO content_cards(dialog_id,owner,max_message_id,kind,data)
            VALUES($1,$2,$3,$4,$5) ON CONFLICT(dialog_id,kind,max_message_id)
            DO UPDATE SET data=excluded.data,updated_at=now() RETURNING *''', dialog['id'], dialog['owner'], str(message_id), kind,
            self.vault.seal(dialog['owner'], f"card:{dialog['id']}:{kind}:{message_id}", data))
