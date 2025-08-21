#!/usr/bin/env python3

print("Starting test...")

try:
    import os
    print("✓ os imported")
    
    import time
    print("✓ time imported")
    
    from dotenv import load_dotenv
    print("✓ dotenv imported")
    
    load_dotenv('.env.local')
    print("✓ .env.local loaded")
    
    from selenium import webdriver
    print("✓ selenium imported")
    
    from webdriver_manager.chrome import ChromeDriverManager
    print("✓ webdriver_manager imported")
    
    from flask import Flask
    print("✓ flask imported")
    
    from flask_sqlalchemy import SQLAlchemy
    print("✓ flask_sqlalchemy imported")
    
    print("All imports successful!")
    
    print("Testing Flask app creation...")
    worker_app = Flask(__name__)
    worker_app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///lms.db')
    worker_app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    print("✓ Flask app created")
    
    worker_db = SQLAlchemy(worker_app)
    print("✓ SQLAlchemy initialized")
    
    print("Testing ChromeDriverManager...")
    chrome_driver_path = ChromeDriverManager().install()
    print(f"✓ ChromeDriver installed at: {chrome_driver_path}")
    
    print("Test completed successfully!")
    
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()