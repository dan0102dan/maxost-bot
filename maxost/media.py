from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp

from .content import safe_name
from .errors import Rejected, RetryLater


def validate_url(url):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None,443):
            raise ValueError()
        try:
            ip = ipaddress.ip_address(p.hostname)
        except ValueError:
            ip = None
        if ip is not None and not ip.is_global:
            raise ValueError()
        if p.hostname.lower() in ('localhost','localhost.localdomain'):
            raise ValueError()
    except (ValueError,TypeError):
        raise Rejected('Небезопасный адрес вложения заблокирован.') from None
    return url


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Validate the very addresses used by the connector, not a separate DNS lookup."""
    def __init__(self):
        self.resolver = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await self.resolver.resolve(host, port, family)
        if not records or any(not ipaddress.ip_address(r['host']).is_global for r in records):
            raise OSError('Non-public media address')
        return records

    async def close(self):
        await self.resolver.close()


class Media:
    def __init__(self, tg, limit):
        self.tg,self.limit=tg,limit
        self.http=None

    async def close(self):
        if self.http:
            await self.http.close()

    async def download(self,url):
        if self.http is None:
            self.http=aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=PublicResolver(),limit=8),
                timeout=aiohttp.ClientTimeout(total=120,connect=15),
                trust_env=False,auto_decompress=False,
            )
        try:
            for _ in range(4):
                validate_url(url)
                async with self.http.get(url,allow_redirects=False,headers={'Accept-Encoding':'identity'}) as response:
                    if response.status in (301,302,303,307,308):
                        url=urljoin(url,response.headers.get('Location',''))
                        continue
                    if response.status in (401,403,404):
                        raise Rejected('Файл MAX недоступен или срок ссылки истёк.')
                    if response.status>=400:
                        raise RetryLater('Не удалось скачать вложение MAX.',15)
                    if response.headers.get('Content-Encoding','identity').lower() != 'identity':
                        raise Rejected('Неожиданное сжатие файла; загрузка заблокирована.')
                    if response.content_length and response.content_length>self.limit:
                        raise Rejected('Файл превышает лимит размера.')
                    chunks,size=[],0
                    async for chunk in response.content.iter_chunked(65536):
                        size+=len(chunk)
                        if size>self.limit:
                            raise Rejected('Файл превышает лимит размера.')
                        chunks.append(chunk)
                    return b''.join(chunks)
            raise Rejected('Слишком много перенаправлений при загрузке файла.')
        except (aiohttp.ClientError,TimeoutError,OSError):
            raise RetryLater('Вложение временно недоступно.',15) from None

    async def for_max(self,a):
        from pymax import File, Photo, Video, VideoNote, Voice
        raw=await self.tg.download(a['file_id'],self.limit)
        kind=a['kind']
        klass={'photo':Photo,'video':Video,'animation':Video,'video_note':VideoNote,'voice':Voice}.get(kind,File)
        if kind == 'sticker' and not a.get('is_animated') and not a.get('is_video'):
            # Arbitrary Telegram sticker sets do not have MAX sticker identifiers.
            # Keep the visible artwork; never upload TGS as if it were a photo.
            import io
            from PIL import Image, UnidentifiedImageError
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width * image.height > 4_000_000:
                        raise Rejected('Изображение стикера слишком большое.')
                    target = io.BytesIO()
                    image.convert('RGBA').save(target, format='PNG')
                    raw = target.getvalue()
                    a = {**a, 'name': 'sticker.png'}
                    klass = Photo
            except (UnidentifiedImageError, OSError, ValueError):
                raise Rejected('Не удалось декодировать стикер Telegram.') from None
        kwargs={'raw':raw,'name':safe_name(a['name'])}
        if kind in ('voice','video_note'):
            kwargs['duration']=a.get('duration',0)
        if kind=='voice' and not raw.startswith(b'OggS'):
            # No fake OGG extensions or silent lossy conversion.
            klass=File
            kwargs.pop('duration',None)
            kwargs['name']='audio.bin'
        return klass(**kwargs)

    async def for_telegram(self,client,chat_id,message_id,index):
        # Refresh short-lived URLs at delivery time; do not persist download URLs.
        message=await client.get_message(chat_id,int(message_id))
        attaches=getattr(message,'attaches',[]) if message else []
        if index>=len(attaches):
            raise Rejected('Исходное вложение удалено или изменено в MAX.')
        a=attaches[index]
        kind=str(getattr(getattr(a,'type',None),'value',getattr(a,'type',''))).upper()
        if kind=='PHOTO':
            url,name,tgkind=a.base_url,'photo.jpg','photo'
        elif kind=='AUDIO':
            url,name,tgkind=a.url,'voice.ogg','voice'
        elif kind=='VIDEO':
            video=await client.get_video_by_id(chat_id,int(message_id),a.video_id)
            url,name,tgkind=getattr(video,'url',None),'video.mp4','video'
        elif kind=='STICKER':
            url,name,tgkind=a.url,'sticker.webp','sticker'
            if getattr(a,'lottie_url',None):
                import gzip
                raw = await self.download(a.lottie_url)
                # Telegram TGS is gzipped Lottie. It can reject unsupported animation features;
                # the delivery layer then sends the original as a clearly labelled document.
                if not raw.startswith(b'\x1f\x8b'):
                    raw = gzip.compress(raw, mtime=0)
                return 'sticker','sticker.tgs',raw
        elif kind=='FILE':
            file=await client.get_file_by_id(chat_id,int(message_id),a.file_id)
            url,name,tgkind=getattr(file,'url',None),safe_name(getattr(a,'name',None)),'document'
        else:
            raise Rejected('Этот тип вложения пока не переносится.')
        if not url:
            raise Rejected('MAX не предоставил адрес вложения.')
        raw=await self.download(url)
        # Photos above the photo endpoint's size budget are still sent as files.
        if tgkind=='photo' and len(raw)>10*1024*1024:
            tgkind='document'
        if tgkind=='voice' and not raw.startswith(b'OggS'):
            tgkind,name='document','audio.bin'
        return tgkind,name,raw
