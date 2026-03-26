from fastapi import FastAPI, APIRouter, HTTPException, Header, UploadFile, File, Request, Response, Query
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Optional
import uuid
from datetime import datetime, timezone, timedelta
import httpx
import requests
import base64

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Storage configuration
STORAGE_URL = "https://integrations.emergentagent.com/objstore/api/v1/storage"
EMERGENT_KEY = os.environ.get("EMERGENT_LLM_KEY")
APP_NAME = "truepixel"
storage_key = None

# Create the main app
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============ Storage Functions ============
def init_storage():
    """Initialize storage and get storage key"""
    global storage_key
    if storage_key:
        return storage_key
    try:
        resp = requests.post(f"{STORAGE_URL}/init", json={"emergent_key": EMERGENT_KEY}, timeout=30)
        resp.raise_for_status()
        storage_key = resp.json()["storage_key"]
        logger.info("Storage initialized successfully")
        return storage_key
    except Exception as e:
        logger.error(f"Storage init failed: {e}")
        return None

def put_object(path: str, data: bytes, content_type: str) -> dict:
    """Upload file to object storage"""
    key = init_storage()
    if not key:
        raise HTTPException(status_code=500, detail="Storage not initialized")
    resp = requests.put(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key, "Content-Type": content_type},
        data=data, timeout=120
    )
    resp.raise_for_status()
    return resp.json()

def get_object(path: str) -> tuple:
    """Download file from object storage"""
    key = init_storage()
    if not key:
        raise HTTPException(status_code=500, detail="Storage not initialized")
    resp = requests.get(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key}, timeout=60
    )
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")

# ============ Models ============
class User(BaseModel):
    user_id: str
    email: str
    name: str
    picture: Optional[str] = None
    created_at: str

class AnalysisResult(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    file_path: str
    file_type: str
    is_fake: bool
    confidence: float
    analysis: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

# ============ Auth Helper ============
async def get_current_user(request: Request, authorization: str = Header(None)) -> User:
    """Get current user from session token"""
    # Check cookie first
    session_token = request.cookies.get("session_token")
    
    # Fallback to Authorization header
    if not session_token and authorization:
        if authorization.startswith("Bearer "):
            session_token = authorization[7:]
    
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # Find session
    session_doc = await db.user_sessions.find_one(
        {"session_token": session_token},
        {"_id": 0}
    )
    
    if not session_doc:
        raise HTTPException(status_code=401, detail="Invalid session")
    
    # Check expiry
    expires_at = session_doc.get("expires_at")
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired")
    
    # Get user
    user_doc = await db.users.find_one(
        {"user_id": session_doc["user_id"]},
        {"_id": 0}
    )
    
    if not user_doc:
        raise HTTPException(status_code=401, detail="User not found")
    
    return User(**user_doc)

# ============ Auth Routes ============
@api_router.post("/auth/session")
async def exchange_session(request: Request, response: Response):
    """Exchange session_id from OAuth for session_token"""
    data = await request.json()
    session_id = data.get("session_id")
    
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    
    # Get user data from Emergent Auth
    async with httpx.AsyncClient() as client_http:
        auth_response = await client_http.get(
            "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
            headers={"X-Session-ID": session_id}
        )
    
    if auth_response.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid session_id")
    
    auth_data = auth_response.json()
    email = auth_data.get("email")
    name = auth_data.get("name")
    picture = auth_data.get("picture")
    session_token = auth_data.get("session_token")
    
    # Create or update user
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    existing_user = await db.users.find_one({"email": email}, {"_id": 0})
    
    if existing_user:
        user_id = existing_user["user_id"]
        await db.users.update_one(
            {"email": email},
            {"$set": {"name": name, "picture": picture}}
        )
    else:
        await db.users.insert_one({
            "user_id": user_id,
            "email": email,
            "name": name,
            "picture": picture,
            "created_at": datetime.now(timezone.utc).isoformat()
        })
    
    # Create session
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    await db.user_sessions.insert_one({
        "user_id": user_id,
        "session_token": session_token,
        "expires_at": expires_at.isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat()
    })
    
    # Set cookie
    response.set_cookie(
        key="session_token",
        value=session_token,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
        max_age=7*24*60*60
    )
    
    user_doc = await db.users.find_one({"user_id": user_id}, {"_id": 0})
    return user_doc

@api_router.get("/auth/me")
async def get_me(request: Request, authorization: str = Header(None)):
    """Get current authenticated user"""
    user = await get_current_user(request, authorization)
    return user.model_dump()

@api_router.post("/auth/logout")
async def logout(request: Request, response: Response):
    """Logout user"""
    session_token = request.cookies.get("session_token")
    if session_token:
        await db.user_sessions.delete_one({"session_token": session_token})
    
    response.delete_cookie(key="session_token", path="/", secure=True, samesite="none")
    return {"message": "Logged out"}

# ============ Upload Routes ============
@api_router.post("/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    authorization: str = Header(None)
):
    """Upload image or video for analysis"""
    user = await get_current_user(request, authorization)  # Verify auth
    
    # Validate file type
    allowed_types = ["image/jpeg", "image/png", "image/webp", "video/mp4", "video/webm", "video/quicktime"]
    content_type = file.content_type or "application/octet-stream"
    
    if content_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"File type not allowed: {content_type}")
    
    # Read file
    file_data = await file.read()
    
    # Generate path
    ext = file.filename.split(".")[-1] if "." in file.filename else "bin"
    file_id = str(uuid.uuid4())
    path = f"{APP_NAME}/uploads/{user.user_id}/{file_id}.{ext}"
    
    # Upload to storage
    try:
        result = put_object(path, file_data, content_type)
        
        # Determine file type
        file_type = "image" if content_type.startswith("image") else "video"
        
        return {
            "file_id": file_id,
            "storage_path": result["path"],
            "file_type": file_type,
            "content_type": content_type,
            "original_filename": file.filename,
            "size": result.get("size", len(file_data))
        }
    except Exception as upload_err:
        logger.error(f"Upload failed: {upload_err}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(upload_err)}")

@api_router.get("/files/{path:path}")
async def download_file(
    path: str,
    request: Request,
    authorization: str = Header(None),
    auth: str = Query(None)
):
    """Download file from storage"""
    # Support query param auth for img tags
    auth_header = authorization or (f"Bearer {auth}" if auth else None)
    await get_current_user(request, auth_header)  # Verify auth
    
    try:
        data, content_type = get_object(path)
        return Response(content=data, media_type=content_type)
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")

# ============ Analysis Routes ============
@api_router.post("/analyze")
async def analyze_media(
    request: Request,
    authorization: str = Header(None)
):
    """Analyze uploaded media for deepfakes using GPT-5.2 Vision"""
    user = await get_current_user(request, authorization)
    
    data = await request.json()
    storage_path = data.get("storage_path")
    file_type = data.get("file_type", "image")
    
    if not storage_path:
        raise HTTPException(status_code=400, detail="storage_path required")
    
    # Only support image analysis for now
    if file_type != "image":
        return {
            "id": str(uuid.uuid4()),
            "is_fake": False,
            "confidence": 0.5,
            "analysis": "Video analysis is not yet supported. Please upload an image for deepfake detection.",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
    
    try:
        # Get image from storage
        image_data, content_type = get_object(storage_path)
        image_base64 = base64.b64encode(image_data).decode('utf-8')
        
        # Analyze with GPT-5.2 Vision
        from emergentintegrations.llm.chat import LlmChat, UserMessage, ImageContent
        
        chat = LlmChat(
            api_key=EMERGENT_KEY,
            session_id=f"analysis_{uuid.uuid4().hex[:8]}",
            system_message="""You are an expert deepfake detection AI. Analyze images for signs of AI manipulation or synthetic generation.

Look for these indicators of deepfakes/AI-generated images:
1. Unnatural skin texture or smoothness
2. Inconsistent lighting or shadows
3. Asymmetric facial features
4. Blurred or distorted backgrounds
5. Artifacts around hair, ears, or edges
6. Unnatural eye reflections or pupils
7. Inconsistent noise patterns
8. Warped text or objects
9. Missing or extra fingers/teeth
10. Unnaturally perfect or symmetric features

Provide your analysis in this exact JSON format:
{
    "is_fake": true/false,
    "confidence": 0.0-1.0,
    "analysis": "Brief explanation of findings"
}

Be precise and analytical. If uncertain, reflect that in your confidence score."""
        ).with_model("openai", "gpt-5.2")
        
        image_content = ImageContent(image_base64=image_base64)
        
        user_message = UserMessage(
            text="Analyze this image for deepfake/AI-generation indicators. Respond ONLY with the JSON format specified.",
            file_contents=[image_content]
        )
        
        response = await chat.send_message(user_message)
        
        # Parse response
        import json
        try:
            # Try to extract JSON from response
            response_text = response.strip()
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]
            result = json.loads(response_text)
        except:
            # Fallback parsing
            is_fake = "fake" in response.lower() or "synthetic" in response.lower() or "generated" in response.lower()
            result = {
                "is_fake": is_fake,
                "confidence": 0.7 if is_fake else 0.6,
                "analysis": response[:500]
            }
        
        return {
            "id": str(uuid.uuid4()),
            "is_fake": result.get("is_fake", False),
            "confidence": min(max(float(result.get("confidence", 0.5)), 0.0), 1.0),
            "analysis": result.get("analysis", "Analysis complete"),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

# ============ Health Check ============
@api_router.get("/")
async def root():
    return {"message": "TruePixel API", "status": "healthy"}

# Include router
app.include_router(api_router)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    try:
        init_storage()
    except Exception as e:
        logger.warning(f"Storage init on startup failed: {e}")

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
