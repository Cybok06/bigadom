import os

from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi


DEFAULT_URI = "mongodb://bigadom73_db_user:0500868021Yaw@ac-0awpzoo-shard-00-00.eb8aosg.mongodb.net:27017,ac-0awpzoo-shard-00-01.eb8aosg.mongodb.net:27017,ac-0awpzoo-shard-00-02.eb8aosg.mongodb.net:27017/?ssl=true&replicaSet=atlas-okzitt-shard-0&authSource=admin&appName=bigadom"
DEFAULT_DB_NAME = "bigadom"


def _mongo_uri() -> str:
    return os.environ.get("MONGODB_URI") or os.environ.get("MONGO_URI") or DEFAULT_URI


def _mongo_db_name() -> str:
    return os.environ.get("MONGODB_DB") or os.environ.get("MONGO_DB_NAME") or DEFAULT_DB_NAME


client = MongoClient(
    _mongo_uri(),
    server_api=ServerApi("1"),
    maxPoolSize=int(os.environ.get("MONGO_MAX_POOL_SIZE", "100")),
    minPoolSize=int(os.environ.get("MONGO_MIN_POOL_SIZE", "0")),
    connectTimeoutMS=int(os.environ.get("MONGO_CONNECT_TIMEOUT_MS", "10000")),
    socketTimeoutMS=int(os.environ.get("MONGO_SOCKET_TIMEOUT_MS", "30000")),
    serverSelectionTimeoutMS=int(os.environ.get("MONGO_SERVER_SELECTION_TIMEOUT_MS", "10000")),
    retryWrites=True,
)

try:
    client.admin.command("ping")
    print("Connected to MongoDB!")
except Exception as e:
    print("MongoDB connection failed:", e)

db = client[_mongo_db_name()]

users_collection = db["users"]
tasks_collection = db["tasks"]
