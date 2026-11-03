import os


os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["DATABASE_URL"] = "postgresql://test:test@localhost/test"
os.environ["GATEWAY_URL"] = "http://localhost:8001"
os.environ["VERIFIER_URL"] = "http://localhost:8002"
os.environ["DEMO_MODE"] = "false"
