# -*- coding: utf-8 -*-
"""把装配好的段写进 admake_segments 和 admake_plot_points。

进来的是 装配.py 的产物，字段名已经换成界面和模板匹配用的那套。段的起止是
Part 视频里的绝对秒，跟播放器和导轨用的是同一条时间轴，不用换算。
拆解管线的原始 structure.json 用「_起 / _止」，这里一并认，省得调用方分两种写法。

每次写之前先清掉这个 Part 原有的段：重新分析就是覆盖，留着旧的会让同一个
Part 出现两套段。剧情点挂在段上，靠外键级联一起走。
"""
import json

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admake import AdmakePlotPoint, AdmakeSegment

# 单独立列的字段，别在 JSON 里重复存一份
已成列 = {'段序号', '起', '止', '段时长', '_起', '_止', '_段时长',
        '这段算哪种戏', '段的尺度档', '剧情点'}


def _数(v, 默=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 默


async def 写段库(db: AsyncSession, part_id: int, 产物: dict) -> dict:
    段们 = 产物.get('段') or []

    旧 = (await db.execute(
        select(AdmakeSegment.id).where(AdmakeSegment.part_id == part_id))).scalars().all()
    if 旧:
        await db.execute(delete(AdmakePlotPoint).where(AdmakePlotPoint.segment_id.in_(旧)))
        await db.execute(delete(AdmakeSegment).where(AdmakeSegment.part_id == part_id))

    点数 = 0
    for i, s in enumerate(段们):
        起 = _数(s.get('起', s.get('_起')))
        止 = _数(s.get('止', s.get('_止')))
        档 = s.get('段的尺度档')
        seg = AdmakeSegment(
            part_id=part_id,
            seg_index=int(s.get('段序号') or (i + 1)),
            start_sec=起,
            end_sec=止,
            duration_sec=_数(s.get('段时长', s.get('_段时长')), 止 - 起),
            seg_type=s.get('这段算哪种戏') or None,
            intensity=int(档) if isinstance(档, (int, float)) else 0,
            fields_json=json.dumps(
                {k: v for k, v in s.items() if k not in 已成列},
                ensure_ascii=False),
        )
        db.add(seg)
        await db.flush()          # 要拿 seg.id 给剧情点挂外键

        for p in (s.get('剧情点') or []):
            db.add(AdmakePlotPoint(
                segment_id=seg.id,
                time_sec=_数(p.get('时间')),
                start_sec=_数(p.get('起'), None) if p.get('起') is not None else None,
                end_sec=_数(p.get('止'), None) if p.get('止') is not None else None,
                kind=p.get('哪一类') or None,
                text=str(p.get('是什么') or ''),
                story_score=_数(p.get('剧情分')),
                intensity_score=int(_数(p.get('尺度分'))),
                fields_json=json.dumps(
                    {k: v for k, v in p.items()
                     if k not in ('时间', '起', '止', '哪一类', '是什么', '剧情分', '尺度分')},
                    ensure_ascii=False),
            ))
            点数 += 1

    await db.commit()
    return {'段': len(段们), '剧情点': 点数}
