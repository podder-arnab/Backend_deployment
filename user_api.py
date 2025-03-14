from flask import Flask, request, jsonify, Blueprint
from werkzeug.security import generate_password_hash, check_password_hash
from pymongo import MongoClient
import jwt
import datetime
from bson import ObjectId
import random
import re

import os
from dotenv import load_dotenv

load_dotenv()
user_api=Blueprint("user_api",__name__)


SECRET_KEY=os.getenv('SECRET_KEY')
VERIFICATION_SECRET_KEY=os.getenv('VERIFICATION_SECRET_KEY')


def get_mongo_client():
    client = MongoClient("mongodb+srv://aniruddhamukherjee:7711@cluster1.vialk.mongodb.net/?retryWrites=true&w=majority&appName=Cluster1")
    return client['scrapper']  # Use 'xyz' as the database name

user_api = Blueprint('user_api', __name__)

@user_api.route('/auth/register', methods=['POST'])
def register_user():
    data = request.json
    name = data.get('name', '').strip()
    username = data.get('username', '').strip()
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()
   
    if not all([name, username, email, password]):
        return jsonify({'error': 'All fields are required'}), 400

    db = get_mongo_client()

    if db['user-profile'].find_one({'email': email}):
        return jsonify({'error': 'Email already exists'}), 409

    if db['user-profile'].find_one({'username': username}):
        return jsonify({'error': 'Username already exists'}), 409

    hashed_password = generate_password_hash(password)
    user_id = db['user-profile'].insert_one({
        'name': name,
        'username': username,
        'email': email,
        'password': hashed_password,
        'email_verified': False,
        'created_at': datetime.datetime.utcnow()
    }).inserted_id

    verification_token = jwt.encode({'user_id': str(user_id)}, VERIFICATION_SECRET_KEY, algorithm='HS256')
    db['email_verifications'].insert_one({'user_id': user_id, 'token': verification_token, 'created_at': datetime.datetime.utcnow()})
    
    return jsonify({'message': 'User registered successfully. Please verify your email.', 'verification_token': verification_token}), 201

@user_api.route('/auth/login', methods=['POST'])
def login_user():
    data = request.json
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    db = get_mongo_client()
    user = db['user-profile'].find_one({'email': email})

    if not user or not check_password_hash(user['password'], password):
        return jsonify({'error': 'Invalid email or password'}), 401

    token = jwt.encode({'user_id': str(user['_id']), 'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=1)}, SECRET_KEY, algorithm='HS256')
    return jsonify({'message': 'Login successful', 'token': token}), 200

@user_api.route('/auth/logout', methods=['POST'])
def logout_user():
    return jsonify({'message': 'Logout successful'}), 200

@user_api.route('/auth/forgot-password', methods=['POST'])
def forgot_password():
    data = request.json
    email = data.get('email', '').strip()
    new_password = data.get('new_password', '').strip()

    if not email or not new_password:
        return jsonify({'error': 'Email and new password are required'}), 400

    db = get_mongo_client()
    user = db['user-profile'].find_one({'email': email})

    if not user:
        return jsonify({'error': 'User with this email does not exist'}), 404

    hashed_password = generate_password_hash(new_password)
    db['user-profile'].update_one({'email': email}, {'$set': {'password': hashed_password}})

    return jsonify({'message': 'Password reset successfully'}), 200

@user_api.route('/verify-email/<token>', methods=['GET'])
def verify_email(token):
    try:
        db = get_mongo_client()
        payload = jwt.decode(token, "VERIFICATION_SECRET_KEY", algorithms=['HS256'])
        user_id = ObjectId(payload['user_id'])
        user = db['user-profile'].find_one({'_id': user_id})
        
        if not user:
            return jsonify({'error': 'User not found'}), 404

        if user.get('email_verified', False):
            return jsonify({'message': 'Email already verified'}), 200

        verification = db['email_verifications'].find_one({'user_id': user_id, 'token': token})
        if not verification:
            return jsonify({'error': 'Invalid verification link'}), 400
        
        db['user-profile'].update_one({'_id': user_id}, {'$set': {'email_verified': True}})
        db['email_verifications'].delete_one({'_id': verification['_id']})
        
        return jsonify({'message': 'Email verified successfully!'}), 200
        
    except jwt.InvalidTokenError:
        return jsonify({'error': 'Invalid verification link'}), 400
    except Exception as e:
        return jsonify({'error': f'Verification failed: {str(e)}'}), 500

@user_api.route('/auth/get-user-details', methods=['GET'])
def get_user_details():
    token = request.headers.get('Authorization')
    if not token or not token.startswith("Bearer "):
        return jsonify({'error': 'Token is missing or invalid'}), 401

    token = token.split(" ")[1]
    try:
        decoded = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        db = get_mongo_client()
        user = db['user-profile'].find_one({'_id': ObjectId(decoded['user_id'])}, {'password': 0})
        
        if not user:
            return jsonify({'error': 'User not found'}), 404
        
        user['_id'] = str(user['_id'])
        return jsonify({'user': user}), 200

    except jwt.ExpiredSignatureError:
        return jsonify({'error': 'Token has expired'}), 401
    except jwt.InvalidTokenError:
        return jsonify({'error': 'Invalid token'}), 401
