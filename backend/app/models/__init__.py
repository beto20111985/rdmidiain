"""
RD Mídia Indoor — Registro de modelos SQLAlchemy.

Importar todos os modelos aqui garante que ``Base.metadata`` (Alembic autogenerate e
harness de testes) conheça todas as tabelas. CADA DOMÍNIO tem sua seção: ao criar
um model novo, adicione o import na seção do seu domínio (não reordene as demais).

Migrations: nesta onda (AÇÃO 002) somente o W1 cria migration (0003). Agentes de
domínio alteram models e registram a necessidade de schema no relatório; um agente
de banco consolida as próximas revisões depois.
"""

# ─── BASE / MIXINS ───────────────────────────────────────────────────────────
from app.models.base import (  # noqa: F401
    BaseModelMixin,
    FullBaseModelMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDMixin,
)

# ─── FUNDAÇÃO: USUÁRIOS / AUTH (W1) ──────────────────────────────────────────
from app.models.users import (  # noqa: F401
    PasswordResetCode,
    RefreshToken,
    User,
    UserInvite,
    UserRecoveryCode,
    UserSession,
)

# ─── FUNDAÇÃO: RBAC (W1) ─────────────────────────────────────────────────────
from app.models.rbac import Permission, Role, RoleAssignment, RolePermission  # noqa: F401

# ─── FUNDAÇÃO: AUDITORIA / CONFIGURAÇÕES / LGPD (W1) ─────────────────────────
from app.models.audit import AuditLog  # noqa: F401
from app.models.lgpd import ConsentRecord, DataDeletionRequest  # noqa: F401
from app.models.settings import SystemSetting, SystemSettingHistory  # noqa: F401

# ─── DOMÍNIO: ORGANIZAÇÕES / ESTABELECIMENTOS / TELAS ────────────────────────
from app.models.organizations import (  # noqa: F401
    AgencyClient,
    AgencyClientGrant,
    BusinessRole,
    DocumentType,
    Organization,
    OrganizationTypeFlag,
)
from app.models.taxonomy import (  # noqa: F401
    AdvertiserProfile,
    AdvertiserSegment,
    Brand,
    CompetitorLink,
    ContentPolicy,
    EstablishmentProduct,
    EstablishmentSegment,
    Product,
    Segment,
    TaxonomyNode,
)
from app.models.contracts import (  # noqa: F401
    ComodatoAgreement,
    ContractAcceptance,
    ContractTemplate,
    PartnerPlan,
)
from app.models.establishments import Establishment  # noqa: F401  (EstablishmentCategory removida → taxonomy_nodes / W-ORG)
from app.models.screens import (  # noqa: F401
    AudienceEstimate,
    Screen,
    ScreenHistory,
    ScreenOrientation,
    ScreenStatus,
    TVOwner,
)
from app.models.installations import (  # noqa: F401
    Installation,
    InstallationPhoto,
    MaintenanceEvent,
    MaintenanceOrder,
)
from app.models.onboarding import OnboardingApplication, OnboardingDocument  # noqa: F401

# ─── DOMÍNIO: MÍDIA ──────────────────────────────────────────────────────────
from app.models.media import MediaFile, MediaFileStatus, MediaKind, MediaOrientation  # noqa: F401  (Creative → app.models.campaigns / W-CAMP)

# ─── DOMÍNIO: CAMPANHAS / SCHEDULER / INVENTÁRIO ─────────────────────────────
from app.models.campaigns import (  # noqa: F401
    Campaign,
    CampaignCreative,
    CampaignRevision,
    CampaignSegment,
    CampaignStatus,
    CampaignStatusHistory,
    CampaignTarget,
    Creative,
    CreativeKind,
    CreativeSegment,
    CreativeStatus,
    OrganizationApprovalPolicy,
    TargetType,
)
from app.models.pricing import (  # noqa: F401
    Coupon,
    CouponRedemption,
    Order,
    OrderItem,
    Plan,
    PriceItem,
    PriceTable,
    Promotion,
)
from app.models.conflicts import ConflictDecision, ConflictException, ConflictOverride, ConflictRule  # noqa: F401
# Inventário por capacidade / manifesto / conteúdo interno (W-INV — ADR_006)
from app.models.rotation import RotationPolicy  # noqa: F401
from app.models.inventory import (  # noqa: F401
    BookingPattern,
    BookingScreen,
    InventoryHold,
    ScreenAnchorHistory,
    ScreenInventoryState,
    ScreenRetirement,
)
from app.models.manifests import ManifestVersion  # noqa: F401
from app.models.content import EmergencyBroadcast, FallbackContent, InternalContent  # noqa: F401

# ─── DOMÍNIO: DISPOSITIVOS / PLAYER (W-PLAY) ─────────────────────────────────
from app.models.devices import (  # noqa: F401
    AppRelease,
    CreativeKill,
    Device,
    DeviceCommand,
    DevicePlatform,
    DeviceSyncEvent,
    DeviceTelemetry,
    PairingRequest,
)

# ─── DOMÍNIO: PLAYBACK / PROOF-OF-PLAY ───────────────────────────────────────
from app.models.playback import PlaybackDailyStat, PlaybackEvent, PlaybackResult  # noqa: F401  (player_manifests → manifest_versions, W-INV)

# ─── DOMÍNIO: FINANCEIRO ─────────────────────────────────────────────────────
from app.models.financial import (  # noqa: F401
    CommissionAccrual,
    CommissionAccrualEvent,
    CommissionCampaign,
    CommissionCampaignScreen,
    CommissionClosing,
    CommissionCompetenceRun,
    CommissionDayPlan,
    CommissionPoolRelease,
    EstablishmentCommissionSetting,
    EstablishmentPixKey,
    FinancialApprovalEvent,
    FinancialApprovalRequest,
    FinancialPasswordResetCode,
    FinancialPeriodClosing,
    FiscalDocumentRequest,
    LedgerAccount,
    LedgerPosting,
    LedgerTransaction,
    Payable,
    PaymentOrder,
    PaymentOrderCallback,
    PaymentOrderStatus,
    ReconciliationItem,
    ReconciliationRun,
    Refund,
    RevenueSchedule,
    WebhookEvent,
    Withdrawal,
)

# ─── DOMÍNIO: EQUIPAMENTOS ───────────────────────────────────────────────────
from app.models.equipment import EquipmentItem, EquipmentMovement, EquipmentStatus, EquipmentType  # noqa: F401

# ─── DOMÍNIO: CRM ────────────────────────────────────────────────────────────
from app.models.crm import (  # noqa: F401
    Lead,
    LeadActivity,
    LeadStage,
    LeadStageEvent,
    LeadStatus,
    Proposal,
    ProposalItem,
    ProposalStatus,
    SalesAttribution,
    SalesRep,
    SalesRepKind,
)

# ─── DOMÍNIO: SUPORTE ────────────────────────────────────────────────────────
from app.models.support import Ticket, TicketEvent, TicketMessage, TicketPriority, TicketStatus  # noqa: F401

# ─── DOMÍNIO: NOTIFICAÇÕES ───────────────────────────────────────────────────
from app.models.notifications import (  # noqa: F401
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationCooldown,
    NotificationDelivery,
    NotificationPreference,
    NotificationTemplate,
)

# ─── DOMÍNIO: DESIGNER / RELATÓRIOS / CONTROL PLANE ──────────────────────────
from app.models.designer import (  # noqa: F401
    DesignApproval,
    DesignAttachment,
    DesignComment,
    DesignEvent,
    DesignExtraRevision,
    DesignRequest,
    DesignStatus,
    DesignVersion,
)
from app.models.instance import InstanceProfile, InstanceSigningKey, RdPriceFloor, RdPriceFloorHistory  # noqa: F401
from app.models.templates import DesignTemplate, TemplateAsset, TemplateRender  # noqa: F401
from app.models.reports import DeliveryProof, ReportExport, ReportRun  # noqa: F401
from app.models.control_plane import (  # noqa: F401
    CpEvent,
    CpInstance,
    CpInstanceGroup,
    CpRelease,
    CpRollout,
    CpRolloutTarget,
)

# ─── SEGURANÇA DA INSTÂNCIA: MODO DE APROVAÇÃO / ADMIN MASTER (AÇÃO 003 — agente E) ─
from app.models.security import (  # noqa: F401
    AdminMasterInvite,
    AdminMasterSeatEvent,
    InstanceApprovalModeHistory,
)
