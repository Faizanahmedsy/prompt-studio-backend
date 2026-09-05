"""Enumerations shared across modules.

`str`-valued so they serialise straight into JSON and compare equal to the raw
strings that arrive from the client, without an explicit `.value` at every site.
"""

from enum import StrEnum


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"
    RESET = "reset"
    INVITE = "invite"
    VERIFY = "verify"


TOKEN_TYPE_BEARER = "bearer"

# What a personal API token looks like on the wire. The prefix is how the bearer
# dependency tells one from a JWT without trying to decode it — and how a secret
# scanner recognises one in a log or a commit.
API_TOKEN_PREFIX = "pst_"


class GlobalRole(StrEnum):
    """Platform-wide role. Decides who reaches the admin surface."""

    SUPERADMIN = "SUPERADMIN"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"


class ProjectRole(StrEnum):
    """A person's standing on ONE project. Unrelated to their global role.

    Ordered by power — `rank()` turns them into comparable numbers so a check
    reads "at least EDITOR" rather than enumerating every role that qualifies.
    """

    OWNER = "OWNER"
    EDITOR = "EDITOR"
    COMMENTER = "COMMENTER"
    VIEWER = "VIEWER"

    @property
    def rank(self) -> int:
        return _PROJECT_ROLE_RANK[self]

    def at_least(self, other: "ProjectRole") -> bool:
        return self.rank >= other.rank


_PROJECT_ROLE_RANK: dict[ProjectRole, int] = {
    ProjectRole.VIEWER: 10,
    ProjectRole.COMMENTER: 20,
    ProjectRole.EDITOR: 30,
    ProjectRole.OWNER: 40,
}


class MemberStatus(StrEnum):
    """Whether the invited address has been claimed by a real account yet.

    A project is shared by email, and the person on the other end may not have
    signed up. INVITED rows carry the email with a null `user_id`; registering
    with that address claims them (see `projects.service.claim_invites`).
    """

    INVITED = "INVITED"
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class ActivityType(StrEnum):
    PROJECT_CREATED = "PROJECT_CREATED"
    PROJECT_UPDATED = "PROJECT_UPDATED"
    PROJECT_RENAMED = "PROJECT_RENAMED"
    PROJECT_DELETED = "PROJECT_DELETED"
    PROJECT_RESTORED = "PROJECT_RESTORED"
    MEMBER_INVITED = "MEMBER_INVITED"
    MEMBER_JOINED = "MEMBER_JOINED"
    MEMBER_ROLE_CHANGED = "MEMBER_ROLE_CHANGED"
    MEMBER_REMOVED = "MEMBER_REMOVED"
    VERSION_SAVED = "VERSION_SAVED"
    VERSION_RESTORED = "VERSION_RESTORED"
    COMMENT_ADDED = "COMMENT_ADDED"
    PUBLIC_LINK_ENABLED = "PUBLIC_LINK_ENABLED"
    PUBLIC_LINK_ROTATED = "PUBLIC_LINK_ROTATED"
    PUBLIC_LINK_DISABLED = "PUBLIC_LINK_DISABLED"


class WsMessage(StrEnum):
    """Wire vocabulary for the collaboration socket.

    Client -> server and server -> client share one namespace because most
    messages are echoed: the server relays `cursor` to the other members
    unchanged, and answers `doc.update` with `doc.updated` to everyone.
    """

    # client -> server
    DOC_UPDATE = "doc.update"
    CURSOR = "cursor"
    SELECTION = "selection"
    PING = "ping"
    # server -> client
    HELLO = "hello"
    # Someone else's save. Always carries the document.
    DOC_UPDATED = "doc.updated"
    # Your own save came back accepted. Carries the authoritative version and
    # deliberately NOT the document — echoing it would clobber whatever you
    # typed while the round trip was in flight. A separate type rather than a
    # flag on `doc.updated`, because one message meaning two things is how a
    # client ends up applying its own edit on top of itself.
    DOC_ACK = "doc.ack"
    DOC_CONFLICT = "doc.conflict"
    # The authoritative roster, sent after every join and leave. `member.joined`
    # and `member.left` say what changed; this says what the room now is, which
    # is what a client should render.
    PRESENCE = "presence"
    MEMBER_JOINED = "member.joined"
    MEMBER_LEFT = "member.left"
    PONG = "pong"
    ERROR = "error"
