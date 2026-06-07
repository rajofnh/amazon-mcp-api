# ============================================
# auth.py — JWT Validation for Amazon MCP API
#
# This is the Python equivalent of auth.js
# from Exercise 5. Same six checks, same
# concepts, different language.
#
# We use FastAPI's dependency injection system
# which is cleaner than adding auth checks
# manually to every endpoint handler.
#
# The validate_token function is a FastAPI
# dependency — declare it in an endpoint and
# FastAPI calls it automatically before the
# endpoint handler runs.
# ============================================

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError, ExpiredSignatureError
from typing import Optional
import os
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ============================================
# Configuration
#
# CONCEPT: In production these values come
# from environment variables — never hardcoded.
# For the MCP server calling this API we use
# a shared secret (HS256) — same approach as
# Exercise 5. When we add Auth0 in Stage 6
# we will switch to RS256 with public keys.
# ============================================

JWT_SECRET = os.getenv("JWT_SECRET", "amazon-mcp-super-secret-key-2024")
JWT_ALGORITHM = "HS256"
EXPECTED_AUDIENCE = "https://amazon-mcp-api.onrender.com"
EXPECTED_ISSUER = "https://auth.amazonmcp.com"

# ============================================
# CONCEPT: HTTPBearer
#
# This tells FastAPI to look for a Bearer
# token in the Authorization header of every
# request. It handles the extraction for us.
# We just receive the token string directly.
# ============================================

security_scheme = HTTPBearer()

# ============================================
# User context — what we extract from the token
# and pass to every endpoint handler
# ============================================

@dataclass
class AuthenticatedUser:
    user_id: str
    email: str
    tenant_id: str
    role: str
    scope: str


# ============================================
# THE MAIN VALIDATOR — six checks
#
# This is a FastAPI dependency function.
# Endpoints that need auth declare it like:
#   async def my_endpoint(user: AuthenticatedUser = Depends(validate_token)):
#
# FastAPI calls this before the endpoint runs.
# If it raises an HTTPException — the endpoint
# never runs and the error is returned.
# If it returns an AuthenticatedUser — that
# user object is passed to the endpoint.
# ============================================

async def validate_token(
    credentials: HTTPAuthorizationCredentials = Security(security_scheme)
) -> AuthenticatedUser:

    # Extract the raw token string
    token = credentials.credentials

    # ---- CHECK 1 & 2: Signature and expiry ----
    # jose's jwt.decode() verifies signature
    # and expiry in one call. It raises
    # specific exceptions for each failure
    # so we can return precise error messages.

    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience=EXPECTED_AUDIENCE,
            issuer=EXPECTED_ISSUER,
            options={
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
            }
        )

    except ExpiredSignatureError:
        # Check 2 failed — token has expired
        logger.warning("[AUTH] Rejected: token expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired. Please obtain a new token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    except JWTError as e:
        error_msg = str(e).lower()

        if "audience" in error_msg:
            # Check 3 failed — wrong audience
            logger.warning(f"[AUTH] Rejected: audience mismatch — {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Token audience mismatch. "
                       f"Expected: {EXPECTED_AUDIENCE}",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if "issuer" in error_msg:
            # Check 4 failed — wrong issuer
            logger.warning(f"[AUTH] Rejected: issuer not trusted — {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Token issuer not trusted. "
                       f"Expected: {EXPECTED_ISSUER}",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Check 1 failed — invalid signature or other error
        logger.warning(f"[AUTH] Rejected: invalid token — {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ---- CHECK 5: Scope ----
    # Verify the token has the required scope
    # for accessing this API.
    # We check for amazon:search as the
    # minimum scope for all endpoints.

    token_scope = payload.get("scope", "")
    required_scope = "amazon:search"

    if required_scope not in token_scope.split():
        logger.warning(
            f"[AUTH] Rejected: insufficient scope. "
            f"Has: '{token_scope}' "
            f"Needs: '{required_scope}'"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient permissions. "
                   f"Required scope: '{required_scope}'. "
                   f"Your token has: '{token_scope or 'none'}'.",
        )

    # ---- CHECK 6: Tenant ID ----
    # Every token must have a tenant_id.
    # This is what isolates one user's
    # requests from another's.

    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        logger.warning("[AUTH] Rejected: missing tenant_id claim")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing required tenant_id claim.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ---- ALL SIX CHECKS PASSED ----
    user = AuthenticatedUser(
        user_id=payload.get("sub", "unknown"),
        email=payload.get("email", "unknown"),
        tenant_id=tenant_id,
        role=payload.get("role", "viewer"),
        scope=token_scope,
    )

    logger.info(
        f"[AUTH] Validated: user={user.email} "
        f"tenant={user.tenant_id} "
        f"role={user.role}"
    )

    return user


# ============================================
# Token generator for testing
#
# This generates test tokens for our MCP
# server and headless agent to use.
# In production tokens come from Auth0.
# ============================================

def generate_test_token(
    user_id: str = "mcp-server-001",
    email: str = "mcp@amazonapp.com",
    tenant_id: str = "amazon-mcp",
    role: str = "api-client",
    scope: str = "amazon:search amazon:read",
    expires_hours: int = 24
) -> str:
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)

    payload = {
        "sub": user_id,
        "email": email,
        "tenant_id": tenant_id,
        "role": role,
        "scope": scope,
        "iss": EXPECTED_ISSUER,
        "aud": EXPECTED_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=expires_hours),
    }

    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
