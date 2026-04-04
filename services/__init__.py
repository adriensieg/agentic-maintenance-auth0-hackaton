"""
services/__init__.py
─────────────────────
Re-export all service singletons for convenient imports:

    from services import calendar_service, payment_service, sms_service, ...
"""
from .calendar_service   import calendar_service,   CalendarService
from .warranty_service   import warranty_service,   WarrantyService
from .payment_service    import payment_service,    PaymentService
from .sms_service        import sms_service,        SMSService
from .voice_service      import voice_service,      VoiceService
from .jira_service       import jira_service,       JiraService
from .technician_service import technician_service, TechnicianService

__all__ = [
    "calendar_service",   "CalendarService",
    "warranty_service",   "WarrantyService",
    "payment_service",    "PaymentService",
    "sms_service",        "SMSService",
    "voice_service",      "VoiceService",
    "jira_service",       "JiraService",
    "technician_service", "TechnicianService",
]
