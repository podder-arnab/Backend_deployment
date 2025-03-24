from flask import request, jsonify, Blueprint
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
import datetime
import re
import os
import oracledb
from dotenv import load_dotenv
import logging
import traceback
from functools import wraps

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

# Get configuration from environment variables
ORACLE_USER = os.getenv('ORACLE_USER', 'ANIRUDDHA')
ORACLE_PASSWORD = os.getenv('ORACLE_PASSWORD', 'OracleDatabase&2025')
ORACLE_DSN = os.getenv('ORACLE_DSN', 'jsondb_high')
SECRET_KEY = os.getenv('SECRET_KEY', 'your-secret-key-fallback')
VERIFICATION_SECRET_KEY = os.getenv('VERIFICATION_SECRET_KEY', 'verification-secret-key-fallback')
TOKEN_EXPIRY_HOURS = int(os.getenv('TOKEN_EXPIRY_HOURS', '24'))

# Create the blueprint
user_api = Blueprint('user_api', __name__)

# Initialize connection pool
connection_pool = None

def initialize_connection_pool():
    """Create and initialize the Oracle connection pool"""
    global connection_pool
    
    try:
        connection_pool = oracledb.create_pool(
            user=ORACLE_USER,
            password=ORACLE_PASSWORD,
            dsn=ORACLE_DSN,
            min=2,
            max=10,
            increment=1,
            # encoding="UTF-8",
            wait_timeout=1000,
            max_lifetime_session=28800,
            config_dir="Wallet_jsondb",
            wallet_location="Wallet_jsondb",
            wallet_password=ORACLE_PASSWORD
        )
        logger.info(f"Connection pool created successfully with {connection_pool.min} to {connection_pool.max} connections")
        return connection_pool
    except Exception as e:
        logger.error(f"Error creating connection pool: {e}")
        traceback.print_exc()
        raise

def get_oracle_connection():
    """Get a connection from the pool or create a new connection if pool doesn't exist"""
    global connection_pool
    
    try:
        if connection_pool is None:
            initialize_connection_pool()
        
        return connection_pool.acquire()
    except Exception as e:
        logger.error(f"Error getting connection: {e}")
        traceback.print_exc()
        
        # Fall back to creating a direct connection if pool fails
        try:
            connection = oracledb.connect(
                user=ORACLE_USER,
                password=ORACLE_PASSWORD,
                dsn=ORACLE_DSN,
                config_dir="Wallet_jsondb",
                wallet_location="Wallet_jsondb",
                wallet_password=ORACLE_PASSWORD
            )
            logger.info("Created direct Oracle DB connection (pool failed)")
            return connection
        except Exception as direct_error:
            logger.error(f"Error creating direct connection: {direct_error}")
            traceback.print_exc()
            raise

def initialize_user_tables():
    """Initialize all necessary tables for user authentication if they don't exist"""
    connection = None
    cursor = None
    
    try:
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Define the user profile table
        tables = {
            "USER_PROFILES": """
                CREATE TABLE USER_PROFILES (
                    ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    NAME VARCHAR2(100) NOT NULL,
                    USERNAME VARCHAR2(50) NOT NULL,
                    EMAIL VARCHAR2(100) NOT NULL,
                    PASSWORD VARCHAR2(255) NOT NULL,
                    EMAIL_VERIFIED NUMBER(1) DEFAULT 0,
                    CREATED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    LAST_LOGIN TIMESTAMP,
                    UPDATED_AT TIMESTAMP,
                    PASSWORD_RESET_AT TIMESTAMP,
                    VERIFIED_AT TIMESTAMP,
                    ROLE VARCHAR2(20) DEFAULT 'user',
                    CONSTRAINT UQ_USER_EMAIL UNIQUE (EMAIL),
                    CONSTRAINT UQ_USER_USERNAME UNIQUE (USERNAME)
                )
            """,
            
            "EMAIL_VERIFICATIONS": """
                CREATE TABLE EMAIL_VERIFICATIONS (
                    ID NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    USER_ID NUMBER NOT NULL,
                    TOKEN VARCHAR2(500) NOT NULL,
                    CREATED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    EXPIRES_AT TIMESTAMP,
                    CONSTRAINT FK_USER_ID FOREIGN KEY (USER_ID) REFERENCES USER_PROFILES(ID) ON DELETE CASCADE
                )
            """
        }
        
        # Check and create each table if it doesn't exist
        for table_name, create_sql in tables.items():
            cursor.execute(f"""
                SELECT COUNT(*) 
                FROM USER_TABLES
                WHERE TABLE_NAME = '{table_name}'
            """)
            
            if cursor.fetchone()[0] == 0:
                cursor.execute(create_sql)
                logger.info(f"Created table {table_name}")
                
                # Create necessary indexes
                if table_name == "USER_PROFILES":
                    # Create index on common search fields
                    cursor.execute(f"""
                        CREATE INDEX IDX_{table_name}_EMAIL 
                        ON {table_name} (EMAIL)
                    """)
                    cursor.execute(f"""
                        CREATE INDEX IDX_{table_name}_USERNAME 
                        ON {table_name} (USERNAME)
                    """)
                elif table_name == "EMAIL_VERIFICATIONS":
                    # Create index for token searching
                    cursor.execute(f"""
                        CREATE INDEX IDX_{table_name}_TOKEN 
                        ON {table_name} (TOKEN)
                    """)
                
        connection.commit()
        logger.info("User authentication tables initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing tables: {e}")
        traceback.print_exc()
        if connection:
            connection.rollback()
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# Initialize tables on module load
initialize_user_tables()

def standardize_error_response(error, code=None, status_code=500):
    """
    Create a standardized error response
    
    Args:
        error: The error message or exception
        code: Error code for frontend handling
        status_code: HTTP status code
        
    Returns:
        JSON response with error details and status code
    """
    if isinstance(error, Exception):
        error_message = str(error)
        traceback_str = traceback.format_exc()
    else:
        error_message = error
        traceback_str = None
    
    response = {
        'status': 'error',
        'message': error_message,
        'timestamp': datetime.datetime.now().isoformat()
    }
    
    if code:
        response['code'] = code
        
    if traceback_str and status_code >= 500:
        # Include traceback in response for server errors
        logger.error(f"Server error: {error_message}\n{traceback_str}")
        response['error_details'] = traceback_str
        
    return jsonify(response), status_code

def token_required(f):
    """Decorator for verifying JWT tokens"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get('Authorization')
        
        if not token or not token.startswith("Bearer "):
            return jsonify({
                'status': 'error',
                'message': 'Unauthorized access. Valid token required.',
                'timestamp': datetime.now().isoformat()
            }), 401

        token = token.split(" ")[1]
        
        try:
            decoded = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
            user_id = decoded['user_id']
            
            # Call the decorated function with user_id as first argument
            return f(user_id, *args, **kwargs)
        except jwt.ExpiredSignatureError:
            return jsonify({
                'status': 'error',
                'message': 'Token has expired',
                'timestamp': datetime.now().isoformat()
            }), 401
        except jwt.InvalidTokenError:
            return jsonify({
                'status': 'error',
                'message': 'Invalid token',
                'timestamp': datetime.now().isoformat()
            }), 401
        except Exception as e:
            print(f"Token verification error: {str(e)}")
            return jsonify({
                'status': 'error',
                'message': 'Authentication failed',
                'timestamp': datetime.now().isoformat()
            }), 401
            
    # Make sure to preserve the original function name
    wrapper.__name__ = f.__name__
    return wrapper

def generate_auth_token(user):
    """Generate JWT authentication token"""
    try:
        payload = {
            'user_id': user['id'],
            'name': user['name'],
            'username': user['username'],
            'email': user['email'],
            'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=TOKEN_EXPIRY_HOURS)
        }
        return jwt.encode(payload, SECRET_KEY, algorithm='HS256')
    except Exception as e:
        logger.error(f"Error generating auth token: {e}")
        traceback.print_exc()
        raise

@user_api.route('/auth/register', methods=['POST'])
def register_user():
    connection = None
    cursor = None
    try:
        data = request.json
        name = data.get('name', '').strip()
        username = data.get('username', '').strip()
        email = data.get('email', '').strip().lower()
        password = data.get('password', '').strip()

        # Validate that all fields are provided
        if not all([name, username, email, password]):
            return standardize_error_response('All fields are required', 'VALIDATION_ERROR', 400)

        # Validate email format
        if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
            return standardize_error_response('Invalid email format', 'VALIDATION_ERROR', 400)

        # Validate password format (must contain both letters and numbers, min 8 chars)
        if not re.match(r'^(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]{8,}$', password):
            return standardize_error_response(
                'Password must be at least 8 characters and include letters and numbers',
                'VALIDATION_ERROR',
                400
            )

        connection = get_oracle_connection()
        cursor = connection.cursor()

        # Check if email already exists
        cursor.execute("""
            SELECT COUNT(*) FROM USER_PROFILES WHERE EMAIL = :email
        """, email=email)
        
        if cursor.fetchone()[0] > 0:
            return standardize_error_response('Email already exists', 'EMAIL_EXISTS', 409)

        # Check if username already exists
        cursor.execute("""
            SELECT COUNT(*) FROM USER_PROFILES WHERE USERNAME = :username
        """, username=username)
        
        if cursor.fetchone()[0] > 0:
            return standardize_error_response('Username already exists', 'USERNAME_EXISTS', 409)

        # Hash the password
        hashed_password = generate_password_hash(password)

        # Create a variable to hold the returned ID
        user_id_var = cursor.var(int)

        # Insert the new user into the database with email_verified set to true (no verification needed)
        cursor.execute("""
            INSERT INTO USER_PROFILES (
                NAME, USERNAME, EMAIL, PASSWORD, EMAIL_VERIFIED, 
                CREATED_AT, LAST_LOGIN, ROLE
            ) VALUES (
                :name, :username, :email, :password, 1, 
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'user'
            ) RETURNING ID INTO :user_id
        """, 
            name=name, 
            username=username, 
            email=email, 
            password=hashed_password,
            user_id=user_id_var
        )
        
        # Get the user ID from the returned variable
        user_id = user_id_var.getvalue()
        connection.commit()
        
        # Create user data for token generation
        user_data = {
            'id': user_id,
            'name': name,
            'username': username,
            'email': email
        }
        
        # Generate a JWT token for the new user
        token = generate_auth_token(user_data)

        logger.info(f"User registered successfully: {email}")
        
        # Return success response with the token and user ID
        return jsonify({
            'message': 'User registered successfully',
            'token': token,
            'user_id': str(user_id)
        }), 201
        
    except oracledb.DatabaseError as e:
        error, = e.args
        logger.error(f"Database error in registration: {str(error)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return standardize_error_response(str(error), 'DB_ERROR', 500)
    except Exception as e:
        logger.error(f"Registration error: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return standardize_error_response('Registration failed', 'SERVER_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()
def standardize_error_response(error, code=None, status_code=500):
    """
    Create a standardized error response
    
    Args:
        error: The error message or exception
        code: Error code for frontend handling
        status_code: HTTP status code
        
    Returns:
        JSON response with error details and status code
    """
    if isinstance(error, Exception):
        error_message = str(error)
        traceback_str = traceback.format_exc()
    else:
        error_message = error
        traceback_str = None
    
    response = {
        'status': 'error',
        'message': error_message,
        'timestamp': datetime.now().isoformat()
    }
    
    if code:
        response['code'] = code
        
    if traceback_str and status_code >= 500:
        # Include traceback in response for server errors
        print(f"Server error: {error_message}\n{traceback_str}")
        response['error_details'] = traceback_str
        
    return jsonify(response), status_code
@user_api.route('/auth/login', methods=['POST'])
def login_user():
    connection = None
    cursor = None
    try:
        data = request.json
        email = data.get('email', '').strip().lower()
        password = data.get('password', '').strip()

        if not email or not password:
            return standardize_error_response('Email and password are required', 'VALIDATION_ERROR', 400)

        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Find user by email
        cursor.execute("""
            SELECT ID, NAME, USERNAME, EMAIL, PASSWORD FROM USER_PROFILES
            WHERE EMAIL = :email
        """, email=email)
        
        user_row = cursor.fetchone()
        
        if not user_row or not check_password_hash(user_row[4], password):
            return standardize_error_response('Invalid email or password', 'AUTH_FAILED', 401)

        # Update last login timestamp
        cursor.execute("""
            UPDATE USER_PROFILES SET LAST_LOGIN = CURRENT_TIMESTAMP
            WHERE ID = :user_id
        """, user_id=user_row[0])
        
        connection.commit()

        # Create user object for token generation
        user = {
            'id': user_row[0],
            'name': user_row[1],
            'username': user_row[2],
            'email': user_row[3]
        }
        
        # Generate a JWT token with user details
        token = generate_auth_token(user)

        logger.info(f"User logged in: {email}")
        
        return jsonify({
            'message': 'Login successful',
            'token': token
        }), 200
        
    except oracledb.DatabaseError as e:
        error, = e.args
        logger.error(f"Database error in login: {str(error)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return standardize_error_response(str(error), 'DB_ERROR', 500)
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return standardize_error_response('Login failed', 'SERVER_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@user_api.route('/auth/forgot-password', methods=['POST'])
def forgot_password():
    connection = None
    cursor = None
    try:
        data = request.json
        email = data.get('email', '').strip().lower()
        new_password = data.get('new_password', '').strip()

        if not email or not new_password:
            return standardize_error_response('Email and new password are required', 'VALIDATION_ERROR', 400)

        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Check if user exists
        cursor.execute("""
            SELECT COUNT(*) FROM USER_PROFILES WHERE EMAIL = :email
        """, email=email)
        
        if cursor.fetchone()[0] == 0:
            return standardize_error_response('User with this email does not exist', 'USER_NOT_FOUND', 404)

        # Update the password
        hashed_password = generate_password_hash(new_password)
        cursor.execute("""
            UPDATE USER_PROFILES SET
            PASSWORD = :password,
            PASSWORD_RESET_AT = CURRENT_TIMESTAMP
            WHERE EMAIL = :email
        """, password=hashed_password, email=email)
        
        connection.commit()

        logger.info(f"Password reset for user: {email}")
        
        return jsonify({
            'message': 'Password reset successfully'
        }), 200
        
    except oracledb.DatabaseError as e:
        error, = e.args
        logger.error(f"Database error in password reset: {str(error)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return standardize_error_response(str(error), 'DB_ERROR', 500)
    except Exception as e:
        logger.error(f"Password reset error: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return standardize_error_response('Password reset failed', 'SERVER_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@user_api.route('/verify-email/<token>', methods=['GET'])
def verify_email(token):
    connection = None
    cursor = None
    try:
        # Decode the token
        payload = jwt.decode(token, VERIFICATION_SECRET_KEY, algorithms=['HS256'])
        user_id = payload['user_id']
        
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        # Check if user exists
        cursor.execute("""
            SELECT EMAIL_VERIFIED FROM USER_PROFILES
            WHERE ID = :user_id
        """, user_id=user_id)
        
        user_row = cursor.fetchone()
        
        if not user_row:
            return standardize_error_response('User not found', 'USER_NOT_FOUND', 404)

        if user_row[0] == 1:
            return jsonify({
                'message': 'Email already verified'
            }), 200

        # Check if verification token exists
        cursor.execute("""
            SELECT COUNT(*) FROM EMAIL_VERIFICATIONS
            WHERE USER_ID = :user_id AND TOKEN = :token
        """, user_id=user_id, token=token)
        
        if cursor.fetchone()[0] == 0:
            return standardize_error_response('Invalid verification link', 'INVALID_TOKEN', 400)
        
        # Update user as verified
        cursor.execute("""
            UPDATE USER_PROFILES SET
            EMAIL_VERIFIED = 1,
            VERIFIED_AT = CURRENT_TIMESTAMP
            WHERE ID = :user_id
        """, user_id=user_id)
        
        # Delete the verification record
        cursor.execute("""
            DELETE FROM EMAIL_VERIFICATIONS
            WHERE USER_ID = :user_id AND TOKEN = :token
        """, user_id=user_id, token=token)
        
        connection.commit()
        
        logger.info(f"Email verified for user ID: {user_id}")
        
        return jsonify({
            'message': 'Email verified successfully!'
        }), 200
        
    except jwt.InvalidTokenError:
        return standardize_error_response('Invalid verification link', 'INVALID_TOKEN', 400)
    except oracledb.DatabaseError as e:
        error, = e.args
        logger.error(f"Database error in email verification: {str(error)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return standardize_error_response(str(error), 'DB_ERROR', 500)
    except Exception as e:
        logger.error(f"Email verification error: {str(e)}")
        traceback.print_exc()
        if connection:
            connection.rollback()
        return standardize_error_response(f'Verification failed: {str(e)}', 'SERVER_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@user_api.route('/auth/get-user-details', methods=['GET'])
@token_required
def get_user_details(user_id):
    connection = None
    cursor = None
    try:
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        cursor.execute("""
            SELECT 
                ID, NAME, USERNAME, EMAIL, EMAIL_VERIFIED
            FROM USER_PROFILES
            WHERE ID = :user_id
        """, user_id=int(user_id))
        
        user_row = cursor.fetchone()
        
        if not user_row:
            return standardize_error_response('User not found', 'USER_NOT_FOUND', 404)
        
        # Create user object
        user = {
            '_id': str(user_row[0]),
            'name': user_row[1],
            'username': user_row[2],
            'email': user_row[3],
            'email_verified': bool(user_row[4])
        }
        
        return jsonify({
            'user': user
        }), 200
        
    except oracledb.DatabaseError as e:
        error, = e.args
        logger.error(f"Database error in get user details: {str(error)}")
        traceback.print_exc()
        return standardize_error_response(str(error), 'DB_ERROR', 500)
    except Exception as e:
        logger.error(f"Get user details error: {str(e)}")
        traceback.print_exc()
        return standardize_error_response('Failed to retrieve user details', 'SERVER_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

@user_api.route('/auth/logout', methods=['POST'])
def logout_user():
    # JWT tokens can't be invalidated without a blacklist
    # Just return success response
    return jsonify({
        'message': 'Logout successful'
    }), 200

@user_api.route('/auth/token-refresh', methods=['POST'])
@token_required
def refresh_token(user_id):
    connection = None
    cursor = None
    try:
        connection = get_oracle_connection()
        cursor = connection.cursor()
        
        cursor.execute("""
            SELECT ID, NAME, USERNAME, EMAIL FROM USER_PROFILES
            WHERE ID = :user_id
        """, user_id=int(user_id))
        
        user_row = cursor.fetchone()
        
        if not user_row:
            return standardize_error_response('User not found', 'USER_NOT_FOUND', 404)
            
        # Create user object for token generation
        user = {
            'id': user_row[0],
            'name': user_row[1],
            'username': user_row[2],
            'email': user_row[3]
        }
        
        # Generate new token
        token = generate_auth_token(user)
        
        return jsonify({
            'message': 'Token refreshed successfully',
            'token': token
        }), 200
        
    except oracledb.DatabaseError as e:
        error, = e.args
        logger.error(f"Database error in token refresh: {str(error)}")
        traceback.print_exc()
        return standardize_error_response(str(error), 'DB_ERROR', 500)
    except Exception as e:
        logger.error(f"Token refresh error: {str(e)}")
        traceback.print_exc()
        return standardize_error_response('Failed to refresh token', 'SERVER_ERROR', 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()