from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, nullable=False, default=False)
    submit_banned_until = Column(DateTime(timezone=False), nullable=True)
    ban_reason = Column(Text, nullable=True)
    profile_background_url = Column(String(500), nullable=False, default="")
    profile_image_url = Column(String(500), nullable=False, default="")
    selected_profile_badge_id = Column(Integer, nullable=False, default=0)
    selected_profile_background_id = Column(Integer, nullable=False, default=0)
    profile_message = Column(Text, nullable=False, default="")
    full_name = Column(String(100), nullable=False, default="")
    student_id = Column(String(50), nullable=False, default="")
    must_change_password = Column(Boolean, nullable=False, default=False)
    is_deleted = Column(Boolean, nullable=False, default=False)
    deleted_at = Column(DateTime(timezone=False), nullable=True)
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    ac_rating = Column(Integer, nullable=False, default=0)
    ac_tier = Column(Integer, nullable=False, default=0)
    ac_rating_problem_sum = Column(Integer, nullable=False, default=0)
    ac_rating_solved_bonus = Column(Integer, nullable=False, default=0)
    solved_count = Column(Integer, nullable=False, default=0)


class AlgorithmTag(Base):
    __tablename__ = "algorithm_tags"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(80), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=False, default="")
    is_active = Column(Boolean, nullable=False, default=True)
    order_index = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=False), server_default=func.now())


class Problem(Base):
    __tablename__ = "problems"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)
    input_description = Column(Text, nullable=False)
    output_description = Column(Text, nullable=False)
    time_limit = Column(Integer, nullable=False, default=2)
    memory_limit = Column(Integer, nullable=False, default=256)
    python_time_limit = Column(Integer, nullable=True)
    c_time_limit = Column(Integer, nullable=True)
    cpp_time_limit = Column(Integer, nullable=True)
    java_time_limit = Column(Integer, nullable=True)
    is_contest_only = Column(Boolean, nullable=False, default=False)
    is_public = Column(Boolean, nullable=False, default=True)
    force_private_submission = Column(Boolean, nullable=False, default=False)
    is_judge_ready = Column(Boolean, nullable=False, default=True)
    difficulty = Column(String(50), nullable=False, default="미지정")
    tier = Column(Integer, nullable=False, default=0)
    judge_priority = Column(Integer, nullable=False, default=0)
    tags = Column(String(255), nullable=False, default="")
    source = Column(String(200), nullable=False, default="")
    problem_author = Column(String(200), nullable=False, default="")
    error_finder = Column(String(200), nullable=False, default="")
    typo_finder = Column(String(200), nullable=False, default="")
    allowed_languages = Column(String(200), nullable=False, default="python,c,cpp,java")
    origin_type = Column(String(30), nullable=False, default="regular")
    origin_group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    origin_contest_id = Column(Integer, ForeignKey("contests.id"), nullable=True)
    review_status = Column(String(30), nullable=False, default="none")
    display_code = Column(String(50), nullable=True)

    examples = relationship("ProblemExample", back_populates="problem", cascade="all, delete-orphan", order_by="ProblemExample.order_index")
    notes = relationship("ProblemNote", back_populates="problem", cascade="all, delete-orphan", order_by="ProblemNote.order_index")
    hints = relationship("ProblemHint", back_populates="problem", cascade="all, delete-orphan", order_by="ProblemHint.order_index")
    contest_links = relationship("ContestProblem", back_populates="problem", cascade="all, delete-orphan")


class ProblemNote(Base):
    __tablename__ = "problem_notes"

    id = Column(Integer, primary_key=True, index=True)
    problem_id = Column(Integer, ForeignKey("problems.id"), nullable=False)
    content = Column(Text, nullable=False, default="")
    order_index = Column(Integer, nullable=False, default=0)

    problem = relationship("Problem", back_populates="notes")


class ProblemHint(Base):
    __tablename__ = "problem_hints"

    id = Column(Integer, primary_key=True, index=True)
    problem_id = Column(Integer, ForeignKey("problems.id"), nullable=False)
    content = Column(Text, nullable=False, default="")
    order_index = Column(Integer, nullable=False, default=0)

    problem = relationship("Problem", back_populates="hints")


class Contest(Base):
    __tablename__ = "contests"

    id = Column(Integer, primary_key=True, index=True)
    display_number = Column(Integer, nullable=False, default=0)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)
    start_time = Column(DateTime(timezone=False), nullable=False)
    end_time = Column(DateTime(timezone=False), nullable=False)
    is_public = Column(Boolean, nullable=False, default=True)
    is_ended = Column(Boolean, nullable=False, default=False)
    is_exam_mode = Column(Boolean, nullable=False, default=False)
    hide_ranking = Column(Boolean, nullable=False, default=False)
    result_display_mode = Column(String(30), nullable=False, default="full")
    score_enabled = Column(Boolean, nullable=False, default=False)
    scoreboard_freeze_enabled = Column(Boolean, nullable=False, default=False)
    scoreboard_freeze_minutes = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    problem_links = relationship("ContestProblem", back_populates="contest", cascade="all, delete-orphan", order_by="ContestProblem.order_index")

class ProblemExample(Base):
    __tablename__ = "problem_examples"

    id = Column(Integer, primary_key=True, index=True)
    problem_id = Column(Integer, ForeignKey("problems.id"), nullable=False)
    input_text = Column(Text, nullable=False, default="")
    output_text = Column(Text, nullable=False, default="")
    order_index = Column(Integer, nullable=False, default=0)

    problem = relationship("Problem", back_populates="examples")


class ContestProblem(Base):
    __tablename__ = "contest_problems"

    contest_id = Column(Integer, ForeignKey("contests.id"), primary_key=True)
    problem_id = Column(Integer, ForeignKey("problems.id"), primary_key=True)
    label = Column(String(10), nullable=False, default="A")
    order_index = Column(Integer, nullable=False, default=0)
    exclude_from_ranking = Column(Boolean, nullable=False, default=False)
    score = Column(Integer, nullable=False, default=100)

    contest = relationship("Contest", back_populates="problem_links")
    problem = relationship("Problem", back_populates="contest_links")



class ContestEditorial(Base):
    __tablename__ = "contest_editorials"
    __table_args__ = (UniqueConstraint("contest_id", "problem_id", name="uq_contest_editorial_problem"),)

    id = Column(Integer, primary_key=True, index=True)
    contest_id = Column(Integer, ForeignKey("contests.id"), nullable=False)
    problem_id = Column(Integer, ForeignKey("problems.id"), nullable=False)
    content = Column(Text, nullable=False, default="")
    image_path = Column(String(500), nullable=False, default="")
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    updated_at = Column(DateTime(timezone=False), server_default=func.now(), onupdate=func.now())

    contest = relationship("Contest")
    problem = relationship("Problem")

class Submission(Base):
    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    problem_id = Column(Integer, ForeignKey("problems.id"), nullable=False)
    contest_id = Column(Integer, ForeignKey("contests.id"), nullable=True)
    practice_id = Column(Integer, ForeignKey("group_practices.id"), nullable=True)
    language = Column(String(50), nullable=False)
    code = Column(Text, nullable=False)
    # result: AC / WA / TLE / RE / CE / SE / WAITING 등 사용자에게 보이는 판정
    result = Column(String(50), nullable=False, default="WAITING")
    # judge_status: PENDING / JUDGING / DONE / FAILED
    judge_status = Column(String(30), nullable=False, default="PENDING")
    detail = Column(Text, nullable=True)
    runtime_ms = Column(Integer, nullable=False, default=0)
    memory_kb = Column(Integer, nullable=False, default=0)
    visibility = Column(String(30), nullable=False, default="private")
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    user = relationship("User")
    problem = relationship("Problem")
    contest = relationship("Contest")
    practice = relationship("GroupPractice")

class ContestQuestion(Base):
    __tablename__ = "contest_questions"

    id = Column(Integer, primary_key=True, index=True)
    contest_id = Column(Integer, ForeignKey("contests.id"), nullable=False)
    problem_id = Column(Integer, ForeignKey("problems.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False)
    answer = Column(Text, nullable=True)
    is_public = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    answered_at = Column(DateTime(timezone=False), nullable=True)

    contest = relationship("Contest")
    problem = relationship("Problem")
    user = relationship("User")


class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text, nullable=False, default="")
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_public = Column(Boolean, nullable=False, default=True)
    is_school_group = Column(Boolean, nullable=False, default=False)
    school_group_request_status = Column(String(30), nullable=False, default="none")
    school_group_request_reason = Column(Text, nullable=False, default="")
    school_group_request_file_path = Column(String(500), nullable=False, default="")
    school_group_request_file_name = Column(String(255), nullable=False, default="")
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    owner = relationship("User")
    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan")


class GroupMember(Base):
    __tablename__ = "group_members"

    group_id = Column(Integer, ForeignKey("groups.id"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    role = Column(String(30), nullable=False, default="member")
    joined_at = Column(DateTime(timezone=False), server_default=func.now())

    group = relationship("Group", back_populates="members")
    user = relationship("User")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False, default="")
    message_type = Column(String(50), nullable=False, default="notice")
    related_group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    related_submission_id = Column(Integer, ForeignKey("submissions.id"), nullable=True)
    action_status = Column(String(30), nullable=False, default="none")
    is_read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])
    group = relationship("Group", foreign_keys=[related_group_id])
    submission = relationship("Submission", foreign_keys=[related_submission_id])


class GroupJoinRequest(Base):
    __tablename__ = "group_join_requests"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String(30), nullable=False, default="pending")
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    group = relationship("Group")
    user = relationship("User")


class GroupProblemSet(Base):
    __tablename__ = "group_problem_sets"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    group = relationship("Group")


class GroupContest(Base):
    __tablename__ = "group_contests"

    id = Column(Integer, primary_key=True, index=True)
    display_number = Column(Integer, nullable=False, default=0)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    contest_id = Column(Integer, ForeignKey("contests.id"), nullable=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    group = relationship("Group")
    contest = relationship("Contest")


class GroupPractice(Base):
    __tablename__ = "group_practices"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False, default="")
    start_time = Column(DateTime(timezone=False), nullable=True)
    end_time = Column(DateTime(timezone=False), nullable=True)
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    group = relationship("Group")


class GroupProblemSetProblem(Base):
    __tablename__ = "group_problem_set_problems"

    id = Column(Integer, primary_key=True, index=True)
    problem_set_id = Column(Integer, ForeignKey("group_problem_sets.id"), nullable=False)
    problem_id = Column(Integer, ForeignKey("problems.id"), nullable=False)
    order_index = Column(Integer, nullable=False, default=0)

    problem_set = relationship("GroupProblemSet")
    problem = relationship("Problem")


class GroupPracticeProblem(Base):
    __tablename__ = "group_practice_problems"

    id = Column(Integer, primary_key=True, index=True)
    practice_id = Column(Integer, ForeignKey("group_practices.id"), nullable=False)
    problem_id = Column(Integer, ForeignKey("problems.id"), nullable=False)
    order_index = Column(Integer, nullable=False, default=0)

    practice = relationship("GroupPractice")
    problem = relationship("Problem")


class JudgeJob(Base):
    __tablename__ = "judge_jobs"

    id = Column(Integer, primary_key=True, index=True)
    submission_id = Column(Integer, ForeignKey("submissions.id"), nullable=False, index=True)
    job_type = Column(String(30), nullable=False, default="judge")  # judge / rejudge
    status = Column(String(30), nullable=False, default="QUEUED")  # QUEUED / RUNNING / DONE / FAILED / CANCELED
    priority = Column(Integer, nullable=False, default=0)
    attempts = Column(Integer, nullable=False, default=0)
    worker_name = Column(String(100), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    started_at = Column(DateTime(timezone=False), nullable=True)
    finished_at = Column(DateTime(timezone=False), nullable=True)

    submission = relationship("Submission")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    actor_username = Column(String(50), nullable=False, default="")
    action = Column(String(80), nullable=False, index=True)
    target_type = Column(String(80), nullable=False, default="")
    target_id = Column(Integer, nullable=True)
    summary = Column(Text, nullable=False, default="")
    ip_address = Column(String(80), nullable=False, default="")
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    actor = relationship("User")


class JudgeLog(Base):
    __tablename__ = "judge_logs"

    id = Column(Integer, primary_key=True, index=True)
    submission_id = Column(Integer, ForeignKey("submissions.id"), nullable=True)
    worker_name = Column(String(100), nullable=False, default="worker")
    event = Column(String(50), nullable=False, default="info")
    message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    submission = relationship("Submission")


class BoardPost(Base):
    __tablename__ = "board_posts"

    id = Column(Integer, primary_key=True, index=True)
    display_number = Column(Integer, nullable=False, default=0)
    board_scope = Column(String(30), nullable=False, default="site")  # site / group
    board_type = Column(String(30), nullable=False, default="notice")  # notice / promo / question / request / general
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False, default="")
    is_pinned = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=False), server_default=func.now())
    updated_at = Column(DateTime(timezone=False), server_default=func.now(), onupdate=func.now())

    group = relationship("Group")
    author = relationship("User")


class BoardComment(Base):
    __tablename__ = "board_comments"

    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("board_posts.id"), nullable=False)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=False), server_default=func.now())

    post = relationship("BoardPost")
    author = relationship("User")


class ProfileAsset(Base):
    __tablename__ = "profile_assets"

    id = Column(Integer, primary_key=True, index=True)
    asset_type = Column(String(30), nullable=False, default="badge")  # badge / background
    title = Column(String(120), nullable=False)
    description = Column(Text, nullable=False, default="")
    image_url = Column(String(500), nullable=False, default="")
    icon_text = Column(String(50), nullable=False, default="")
    condition_type = Column(String(30), nullable=False, default="single")  # single / all / any
    condition_problem_ids = Column(Text, nullable=False, default="")
    condition_value = Column(String(120), nullable=False, default="")
    is_default = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=False), server_default=func.now())
