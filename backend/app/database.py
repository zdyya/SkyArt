from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine.sync_engine, "connect")
def _每条连接都这么设(dbapi_conn, _):
    """SQLite 的三个开关，每开一条连接都要设一遍。

    外键：模型里写的 ondelete=CASCADE 在 SQLite 上默认不生效，删项目会留下
    一堆挂在已删 Part 上的段；而 SQLite 的主键会重用已删除的 id，新建的 Part
    拿到同一个 id 就把上一个项目的段认领过来了。
    WAL：读写可以并行，导出后台任务写库的同时用户点开列表不会撞上 database is locked。
    等锁超时：默认 5 秒，慢一点的写就报错，给到 30 秒。
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_tables():
    """Create all tables on startup."""
    from app.models import user, project, episode, asset, storyboard, admake  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Idempotent column additions for existing SQLite databases
        for stmt in [
            "ALTER TABLE users ADD COLUMN role VARCHAR(32) DEFAULT 'admin'",
            "ALTER TABLE admake_projects ADD COLUMN editor_alias VARCHAR(20) DEFAULT ''",
            "ALTER TABLE admake_projects ADD COLUMN export_seq INTEGER DEFAULT 0",
            "ALTER TABLE admake_sources ADD COLUMN width INTEGER",
            "ALTER TABLE admake_sources ADD COLUMN height INTEGER",
            "ALTER TABLE admake_sources ADD COLUMN fps FLOAT",
            "ALTER TABLE admake_sources ADD COLUMN head_trim FLOAT DEFAULT 0",
            "ALTER TABLE admake_sources ADD COLUMN tail_trim FLOAT DEFAULT 0",
            "ALTER TABLE admake_parts ADD COLUMN ai_status VARCHAR(32) DEFAULT 'none'",
            "ALTER TABLE admake_parts ADD COLUMN ai_error VARCHAR(500)",
            # 成片模板：admake_templates 表由 create_all 建；老库补项目的默认模板列
            # （SQLite ALTER 不带 REFERENCES，FK 仅在新库由 create_all 声明，够用）
            "ALTER TABLE admake_projects ADD COLUMN default_template_id INTEGER",
            # 自定义导出目录（本地绝对路径），空=默认导出目录
            "ALTER TABLE admake_projects ADD COLUMN export_path VARCHAR(500)",
            # 模板套用失败原因（导出成功但模板没套上时非空）
            "ALTER TABLE admake_exports ADD COLUMN template_error VARCHAR(300)",
            # 这一条导出为什么失败。界面上原来只显示「失败」两个字
            "ALTER TABLE admake_exports ADD COLUMN fail_reason VARCHAR(500)",
            # AI 分析跑完的时间，首页「今天分析完几个 Part」按它数
            "ALTER TABLE admake_parts ADD COLUMN ai_done_at DATETIME",
            # 用户的默认模板：新建项目和导出面板没选模板时预填它
            "ALTER TABLE admake_templates ADD COLUMN is_default BOOLEAN DEFAULT 0",
            # 这批导出用的目标时长。以前只写进成片旁边的 .log.txt，数据库里查不到，
            # 导致「按你常用的时长给快捷值」这件事没有数据来源。
            "ALTER TABLE admake_exports ADD COLUMN target_duration FLOAT",
            # 项目类型：老库全部项目补成 marks（按高光钩子剪），行为与加列之前一致
            "ALTER TABLE admake_projects ADD COLUMN project_kind VARCHAR(32) DEFAULT 'marks'",
            # 这一批成片用的剪辑模板，和每一格填了哪个段。换段、复现、回流都要它
            "ALTER TABLE admake_exports ADD COLUMN cut_template_id INTEGER",
            "ALTER TABLE admake_exports ADD COLUMN plan_json TEXT",
        ]:
            try:
                await conn.execute(__import__("sqlalchemy").text(stmt))
            except Exception:
                pass  # column already exists
        # 按模板剪只有 qwen 拆段一条管线。这一列早先跟着前端的默认值存成了 gemini，
        # 实际跑的一直是 qwen——库里的值是错的，界面上照着显示就是假的。
        try:
            from app.services.decompose.环境 import 分析模型
            await conn.execute(__import__("sqlalchemy").text(
                "UPDATE admake_projects SET ai_model = :m "
                "WHERE project_kind = 'segments' AND ai_model NOT LIKE 'qwen%'"
            ), {"m": 分析模型()})
        except Exception:
            pass
        # 清掉挂在已删除 Part 上的段和剧情点。删项目原来不清段，而 SQLite 会重用
        # 已删除的 id，新建的 Part 拿到同一个 id 就把上一个项目的段认领过来：
        # 界面上看到别人的段，「只分析没分析过的」还会跳过这些 Part。
        try:
            t = __import__("sqlalchemy").text
            await conn.execute(t(
                "DELETE FROM admake_plot_points WHERE segment_id NOT IN "
                "(SELECT id FROM admake_segments)"))
            r = await conn.execute(t(
                "DELETE FROM admake_segments WHERE part_id NOT IN "
                "(SELECT id FROM admake_parts)"))
            if r.rowcount:
                print(f"🧹  清掉 {r.rowcount} 条挂在已删除 Part 上的段")
        except Exception:
            pass
