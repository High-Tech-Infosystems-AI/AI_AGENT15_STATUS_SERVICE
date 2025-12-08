from fastapi import FastAPI
from app.api import status_api
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Status API",
    version="1.0.0",
    description="Status Service API",
    docs_url="/model/api/docs"
)
# Configure CORS middleware to allow cross-origin requests
origins = ["*"]  # Allow requests from all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Include status API router with WebSocket endpoints
app.include_router(status_api.router)




