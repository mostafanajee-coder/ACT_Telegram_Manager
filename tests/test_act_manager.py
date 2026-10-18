"""No network, account credentials, or Telegram imports required."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from act_manager import policy

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
CHAT_1 = -1001234567890
CHAT_2 = -1001234567891
CHAT_3 = -1001234567892
CHAT_4 = -1001234567893

@pytest.fixture
def paths(tmp_path):
    config=tmp_path/'act_config.json'
    db=tmp_path/'posts.sqlite3'
    config.write_text(json.dumps({
        'expected_user_id': 12345678,
        'daily_ad_limit': 3,
        'allowed_chats': [
            {'chat_id':CHAT_1,'label':'Salla','ads_allowed':True},
            {'chat_id':CHAT_2,'label':'Odoo','ads_allowed':True},
            {'chat_id':CHAT_3,'label':'ERP','ads_allowed':True},
            {'chat_id':CHAT_4,'label':'Replies only','ads_allowed':False},
        ]
    }), encoding='utf8')
    return config,db


def prep(paths,chat_id=CHAT_1,kind='ad',content='A',reply_to=None,schedule_at=None,now=NOW):
    config,db=paths
    return policy.prepare(chat_id=chat_id,kind=kind,content=content,
        reply_to=reply_to,schedule_at=schedule_at,config_path=config,db_path=db,now=now)

async def fake_send(post):
    return 42

async def dispatch(paths, id,now=NOW,sender=fake_send):
    config,db=paths
    return await policy.execute(id,sender,config_path=config,db_path=db,now=now)


def approve(paths, post):
    return policy.approve(post['id'],db_path=paths[1])


def test_empty_config_denies(tmp_path):
    config=tmp_path/'cfg.json'
    config.write_text(json.dumps(policy.config_example()))
    with pytest.raises(policy.PolicyError,match='allowlist'):
        policy.prepare(chat_id=CHAT_1,kind='ad',content='x',config_path=config,db_path=tmp_path/'state.db')


def test_allowed_chat_and_ads_flag(paths):
    with pytest.raises(policy.PolicyError,match='allowlist'):
        prep(paths,chat_id=-1005555555555)
    with pytest.raises(policy.PolicyError,match='not authorized'):
        prep(paths,chat_id=CHAT_4)
    assert prep(paths,chat_id=CHAT_4,kind='reply',reply_to=1)['status']=='draft'


def test_no_send_before_approval(paths):
    post=prep(paths)
    with pytest.raises(policy.PolicyError,match='separate local approval'):
        asyncio.run(dispatch(paths,post['id']))
    assert policy.get_post(post['id'],db_path=paths[1])['status']=='draft'


def test_reply_requirements(paths):
    with pytest.raises(policy.PolicyError,match='original message'):
        prep(paths,kind='reply')
    with pytest.raises(policy.PolicyError,match='cannot be replies'):
        prep(paths,reply_to=12)
    with pytest.raises(policy.PolicyError,match='disabled'):
        prep(paths,kind='reply',reply_to=12,schedule_at=(NOW+HOUR).isoformat())
    post=prep(paths,kind='reply',reply_to=12)
    approve(paths,post)
    result=asyncio.run(dispatch(paths,post['id']))
    assert result['status']=='sent' and result['external_id']==42


def test_ad_limit_and_spacing(paths):
    base=NOW + HOUR
    for i,chat in enumerate((CHAT_1,CHAT_2,CHAT_3)):
        post=prep(paths,chat_id=chat,content=f'Ad {i}',schedule_at=(base+timedelta(hours=i*2)).isoformat())
        approve(paths,post)
        assert asyncio.run(dispatch(paths,post['id']))['status']=='scheduled'
    p4=prep(paths,chat_id=CHAT_4,kind='reply',reply_to=222,content='Answer')
    approve(paths,p4)
    assert asyncio.run(dispatch(paths,p4['id']))['status']=='sent'
    extra=prep(paths,chat_id=CHAT_1,content='Fourth',schedule_at=(base+timedelta(hours=6)).isoformat())
    approve(paths,extra)
    with pytest.raises(policy.PolicyError,match='quota'):
        asyncio.run(dispatch(paths,extra['id']))


def test_spacing_across_chats(paths):
    base=NOW+HOUR
    a=prep(paths,schedule_at=base.isoformat())
    approve(paths,a)
    asyncio.run(dispatch(paths,a['id']))
    b=prep(paths,chat_id=CHAT_2,content='Another',schedule_at=(base+timedelta(minutes=15)).isoformat())
    approve(paths,b)
    with pytest.raises(policy.PolicyError,match='60 minutes'):
        asyncio.run(dispatch(paths,b['id']))


def test_duplicate_and_replay(paths):
    a=prep(paths)
    approve(paths,a)
    assert asyncio.run(dispatch(paths,a['id']))['status']=='sent'
    with pytest.raises(policy.PolicyError,match='unused'):
        asyncio.run(dispatch(paths,a['id']))
    b=prep(paths)
    approve(paths,b)
    with pytest.raises(policy.PolicyError,match='duplicate'):
        asyncio.run(dispatch(paths,b['id'],now=NOW+timedelta(minutes=61)))


def test_ambiguous_failure_blocks_retry(paths):
    a=prep(paths)
    approve(paths,a)
    async def bad_sender(post):
        raise TimeoutError('connection dropped')
    with pytest.raises(policy.PolicyError,match='uncertain'):
        asyncio.run(dispatch(paths,a['id'],sender=bad_sender))
    assert policy.get_post(a['id'],db_path=paths[1])['status']=='uncertain'
    with pytest.raises(policy.PolicyError,match='unused'):
        asyncio.run(dispatch(paths,a['id']))


def test_config_changes_fail_closed(paths):
    a=prep(paths)
    approve(paths,a)
    cfg=json.loads(paths[0].read_text()); cfg['allowed_chats']=[]
    paths[0].write_text(json.dumps(cfg))
    with pytest.raises(policy.PolicyError,match='allowlist'):
        asyncio.run(dispatch(paths,a['id']))


def test_requires_identity(paths):
    a=prep(paths)
    approve(paths,a)
    cfg=json.loads(paths[0].read_text());cfg['expected_user_id']=0
    paths[0].write_text(json.dumps(cfg))
    with pytest.raises(policy.PolicyError,match='expected_user_id'):
        asyncio.run(dispatch(paths,a['id']))


def test_timezone_is_explicit(paths):
    with pytest.raises(policy.PolicyError,match='Timezone'):
        prep(paths,schedule_at='2026-10-17T16:00:00')
    with pytest.raises(policy.PolicyError,match='7 days'):
        prep(paths,schedule_at='2026-11-17T16:00:00+03:00')


def test_cancel_and_no_double_approval(paths):
    post=prep(paths)
    approve(paths,post)
    with pytest.raises(policy.PolicyError,match='unapproved'):
        approve(paths,post)
    assert policy.cancel(post['id'],db_path=paths[1])['status']=='cancelled'
    with pytest.raises(policy.PolicyError,match='unused'):
        asyncio.run(dispatch(paths,post['id']))


def test_config_validation(paths):
    data=json.loads(paths[0].read_text());data['daily_ad_limit']=50
    paths[0].write_text(json.dumps(data))
    with pytest.raises(policy.PolicyError,match='from 1 to 3'):
        prep(paths)


def test_exact_text_kept(paths):
    msg='عرض الربط المحاسبي بين سلة و Odoo - تفاصيل\nخصم محدود.'
    post=prep(paths,content=msg)
    assert post['content']==msg


def test_config_mismatch_reserved(paths):
    a=prep(paths)
    approve(paths,a)
    cfg=json.loads(paths[0].read_text());cfg['allowed_chats'][0]['ads_allowed']=False
    paths[0].write_text(json.dumps(cfg))
    with pytest.raises(policy.PolicyError,match='not authorized'):
        asyncio.run(dispatch(paths,a['id']))


def test_concurrent_dispatch_cannot_send_twice(paths):
    a=prep(paths)
    approve(paths,a)
    counter=[]
    async def send_once(post):
        counter.append(post['id'])
        await asyncio.sleep(0)
        return 88
    async def do_both():
        return await asyncio.gather(dispatch(paths,a['id'],sender=send_once),
                                    dispatch(paths,a['id'],sender=send_once),
                                    return_exceptions=True)
    results=asyncio.run(do_both())
    assert len(counter)==1
    assert sum(isinstance(r,dict) for r in results)==1
    assert sum(isinstance(r,policy.PolicyError) for r in results)==1


def test_quota_uses_riyadh_calendar_day(paths):
    # 2026-10-18 00:15 Riyadh = 2026-10-17 21:15 UTC
    post=prep(paths,content='Late evening',schedule_at='2026-10-18T00:15:00+03:00')
    approve(paths,post)
    asyncio.run(dispatch(paths,post['id']))
    # Still the same Riyadh calendar day, different UTC day from previous.
    post2=prep(paths,chat_id=CHAT_2,content='Morning',schedule_at='2026-10-18T03:45:00+03:00')
    approve(paths,post2)
    asyncio.run(dispatch(paths,post2['id']))
    post3=prep(paths,chat_id=CHAT_3,content='Afternoon',schedule_at='2026-10-18T10:00:00+03:00')
    approve(paths,post3)
    asyncio.run(dispatch(paths,post3['id']))
    post4=prep(paths,chat_id=CHAT_1,content='Too many',schedule_at='2026-10-18T12:00:00+03:00')
    approve(paths,post4)
    with pytest.raises(policy.PolicyError,match='quota'):
        asyncio.run(dispatch(paths,post4['id']))


def test_no_reply_flood_quotas(paths):
    # Replies are human-approved but do not consume advertisement quota.
    for i in range(6):
        post=prep(paths,kind='reply',reply_to=100+i,content=f'Answer {i}')
        approve(paths,post)
        assert asyncio.run(dispatch(paths,post['id']))['status']=='sent'


def test_hidden_config_never_exported(paths):
    # Policy status and draft summaries do not return Telegram credentials.
    posted=prep(paths)
    exported=json.dumps(policy.get_post(posted['id'],db_path=paths[1]))
    assert 'TELEGRAM_SESSION_STRING' not in exported
    assert 'TELEGRAM_API_HASH' not in exported
