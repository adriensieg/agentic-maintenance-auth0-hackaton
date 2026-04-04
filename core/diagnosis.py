"""
core/diagnosis.py
──────────────────
Appliance fault diagnosis engine.

Combines:
  1. Rule-based fault-code lookup (Samsung, LG, Bosch, Whirlpool).
  2. Optional vision analysis of a user-uploaded photo.
  3. Confidence scoring.

Returns a DiagnosisResult with the identified part number and description.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Optional

from models import DiagnosisResult

logger = logging.getLogger("washfix.core.diagnosis")

# ── Samsung fault code database ───────────────────────────────────────────
SAMSUNG_FAULTS: dict[str, dict[str, str]] = {
    "4E": {
        "description": "Water inlet failure — machine could not fill. "
                       "Typically caused by a stuck/faulty inlet solenoid valve "
                       "or a kinked supply hose.",
        "part_number": "DC62-00142A",
        "part_name":   "Water Inlet Solenoid Valve",
    },
    "5E": {
        "description": "Drainage failure — water not draining. "
                       "Check drain pump filter, drain hose, and pump motor.",
        "part_number": "DC97-16934B",
        "part_name":   "Drain Pump Assembly",
    },
    "DC": {
        "description": "Door open / unbalanced load. Door latch or harness fault.",
        "part_number": "DC64-00519B",
        "part_name":   "Door Latch Assembly",
    },
    "UE": {
        "description": "Unbalanced load during spin. May indicate worn drum bearings.",
        "part_number": "DC97-16151B",
        "part_name":   "Drum Bearing & Seal Kit",
    },
    "HE": {
        "description": "Heating element fault. Water not reaching target temperature.",
        "part_number": "DC47-00018A",
        "part_name":   "Heating Element Assembly",
    },
    "DE": {
        "description": "Door error. Door not fully closing or latch sensor fault.",
        "part_number": "DC64-00519B",
        "part_name":   "Door Latch Assembly",
    },
    "OE": {
        "description": "Overflow error. Water level sensor or inlet valve stuck open.",
        "part_number": "DC62-00142A",
        "part_name":   "Water Inlet Solenoid Valve",
    },
}

# Generic symptom patterns → fault code mapping
SYMPTOM_KEYWORDS: dict[str, str] = {
    "buzzing":      "4E",
    "loud buzz":    "4E",
    "not filling":  "4E",
    "no water":     "4E",
    "not draining": "5E",
    "standing water": "5E",
    "door":         "DC",
    "won't close":  "DC",
    "shaking":      "UE",
    "vibrating":    "UE",
    "no heat":      "HE",
    "cold water":   "HE",
}


class DiagnosisEngine:
    """
    Diagnose washing machine faults from error codes, symptom text,
    and optional photo analysis.
    """

    async def diagnose(
        self,
        fault_codes: list[str],
        symptoms: str,
        appliance_model: str,
        photo_bytes: Optional[bytes] = None,
    ) -> DiagnosisResult:
        """
        Main entry point.  Returns a DiagnosisResult.

        Priority: explicit fault code > symptom keywords > photo analysis.
        """
        # 1. Try fault codes
        for code in fault_codes:
            code_upper = code.strip().upper()
            if code_upper in SAMSUNG_FAULTS:
                data = SAMSUNG_FAULTS[code_upper]
                logger.info(f"Diagnosis matched fault code {code_upper}.")
                result = DiagnosisResult(
                    fault_code   = code_upper,
                    description  = data["description"],
                    part_number  = data["part_number"],
                    part_name    = data["part_name"],
                    confidence   = 0.95,
                )
                if photo_bytes:
                    result = await self._enrich_with_photo(result, photo_bytes, appliance_model)
                return result

        # 2. Try symptom keywords
        symptoms_lower = symptoms.lower()
        for keyword, code in SYMPTOM_KEYWORDS.items():
            if keyword in symptoms_lower and code in SAMSUNG_FAULTS:
                data = SAMSUNG_FAULTS[code]
                logger.info(f"Diagnosis matched symptom keyword '{keyword}' → {code}.")
                result = DiagnosisResult(
                    fault_code   = code,
                    description  = data["description"],
                    part_number  = data["part_number"],
                    part_name    = data["part_name"],
                    confidence   = 0.75,
                )
                if photo_bytes:
                    result = await self._enrich_with_photo(result, photo_bytes, appliance_model)
                return result

        # 3. Photo-only analysis
        if photo_bytes:
            result = await self._analyse_photo_only(photo_bytes, appliance_model)
            if result:
                return result

        # 4. Unknown fallback
        logger.warning("Diagnosis: no match found — returning unknown fault.")
        return DiagnosisResult(
            fault_code  = "UNKNOWN",
            description = "Unable to identify specific fault. Manual inspection required.",
            part_number = "N/A",
            part_name   = "Unknown",
            confidence  = 0.0,
        )

    async def _enrich_with_photo(
        self,
        result: DiagnosisResult,
        photo_bytes: bytes,
        model: str,
    ) -> DiagnosisResult:
        """
        Use Claude Vision to confirm / refine the existing diagnosis.
        """
        try:
            import anthropic
            from config.settings import get_settings
            s = get_settings()
            if not s.anthropic_api_key:
                return result

            client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
            b64 = base64.standard_b64encode(photo_bytes).decode("utf-8")

            prompt = (
                f"This is a photo of a {model} washing machine. "
                f"We have diagnosed fault code {result.fault_code} "
                f"({result.description}). "
                f"Look at the image and confirm or correct this diagnosis. "
                f"Specifically check for signs of water leakage, component damage, "
                f"or anything inconsistent with the stated fault. "
                f"Reply in one sentence: either CONFIRMED or CORRECTED: <new_fault_code>."
            )
            msg = await client.messages.create(
                model     = "claude-opus-4-6",
                max_tokens= 150,
                messages  = [{
                    "role":    "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                        {"type": "text",  "text":   prompt},
                    ],
                }],
            )
            text = msg.content[0].text.strip()
            logger.info(f"Photo enrichment: {text}")

            if text.startswith("CONFIRMED"):
                result.confidence = min(result.confidence + 0.04, 1.0)
            elif text.startswith("CORRECTED:"):
                new_code = text.split(":", 1)[1].strip().upper()
                if new_code in SAMSUNG_FAULTS:
                    data = SAMSUNG_FAULTS[new_code]
                    result.fault_code  = new_code
                    result.description = data["description"]
                    result.part_number = data["part_number"]
                    result.part_name   = data["part_name"]
                    result.confidence  = 0.88
        except Exception as exc:
            logger.warning(f"Photo enrichment failed ({exc}) — using code-only diagnosis.")

        return result

    async def _analyse_photo_only(
        self,
        photo_bytes: bytes,
        model: str,
    ) -> Optional[DiagnosisResult]:
        """
        Attempt to diagnose purely from a photo using Claude Vision.
        """
        try:
            import anthropic
            from config.settings import get_settings
            s = get_settings()
            if not s.anthropic_api_key:
                return None

            client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
            b64 = base64.standard_b64encode(photo_bytes).decode("utf-8")

            prompt = (
                f"This is a photo of a {model} washing machine. "
                f"Identify the most likely fault. "
                f"Respond ONLY with the Samsung fault code (e.g. 4E, 5E, DC) "
                f"and nothing else. If you cannot identify a fault code, respond UNKNOWN."
            )
            msg = await client.messages.create(
                model     = "claude-opus-4-6",
                max_tokens= 20,
                messages  = [{
                    "role":    "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                        {"type": "text",  "text":   prompt},
                    ],
                }],
            )
            code = msg.content[0].text.strip().upper()
            if code in SAMSUNG_FAULTS:
                data = SAMSUNG_FAULTS[code]
                return DiagnosisResult(
                    fault_code  = code,
                    description = data["description"],
                    part_number = data["part_number"],
                    part_name   = data["part_name"],
                    confidence  = 0.70,
                )
        except Exception as exc:
            logger.warning(f"Photo-only diagnosis failed: {exc}")
        return None


# Singleton
diagnosis_engine = DiagnosisEngine()
