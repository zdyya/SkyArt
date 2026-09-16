from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

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
            # 用户的默认模板：新建项目和导出面板没选模板时预填它
            "ALTER TABLE admake_templates ADD COLUMN is_default BOOLEAN DEFAULT 0",
            # 这批导出用的目标时长。以前只写进成片旁边的 .log.txt，数据库里查不到，
            # 导致「按你常用的时长给快捷值」这件事没有数据来源。
            "ALTER TABLE admake_exports ADD COLUMN target_duration FLOAT",
            # 项目类型：老库全部项目补成 marks（按高光钩子剪），行为与加列之前一致
            "ALTER TABLE admake_projects ADD COLUMN project_kind VARCHAR(16) DEFAULT 'marks'",
        ]:
            try:
                await conn.execute(__import__("sqlalchemy").text(stmt))
            except Exception:
                pass  # column already exists
