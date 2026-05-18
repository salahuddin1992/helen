# CommClient Server Test Framework

Comprehensive test suite for CommClient-Server backend with 151 tests covering REST API endpoints, security, presence tracking, and core business logic.

## Test Coverage

### Core Test Modules (7 files)

#### 1. **test_auth.py** — Authentication Endpoints (27 tests)
- **TestRegister**: User registration with validation
  - Success cases with/without optional fields
  - Duplicate username, weak password, invalid username formats
  - Field validation (min/max lengths, required fields)
  
- **TestLogin**: User authentication
  - Success with correct credentials
  - Error cases (wrong password, nonexistent user)
  - Optional device_name field
  
- **TestRefreshToken**: Token refresh mechanism
  - Valid refresh token returns new access token
  - Invalid/expired token handling
  
- **TestLogout**: Session termination
  - Logout with and without refresh token
  - Authentication requirement
  
- **TestAuthFlow**: Integration tests
  - Full register → login → refresh cycle
  - Multiple logins from same user

#### 2. **test_users.py** — User Management (35 tests)
- **TestUserProfile**: Profile retrieval and updates
  - Get current user (`GET /api/users/me`)
  - Update fields (display_name, avatar_url, bio, status)
  - Status validation (online/away/busy/dnd)
  
- **TestUserListing**: User discovery
  - List all users with pagination
  - Search functionality
  - Limit and skip parameters
  
- **TestGetUserById**: Individual user lookup
  - Get specific user by ID
  - Handle nonexistent users
  
- **TestContacts**: Contact management
  - Add/remove/list contacts
  - Contact nicknames and blocking
  - Favorite contacts
  - Contact metadata (created_at, status)

#### 3. **test_channels.py** — Channel Management (26 tests)
- **TestCreateChannel**: Channel creation
  - DM (direct message) channels
  - Group channels with name/description
  - Multiple members and member_ids
  - Type validation
  
- **TestListChannels**: Channel discovery
  - List user's channels
  - Empty channel lists
  
- **TestGetChannel**: Channel retrieval
  - Get specific channel with members
  - Member count and info
  
- **TestUpdateChannel**: Channel modifications
  - Update name, description, avatar_url
  - Permission checks
  
- **TestChannelMembers**: Member management
  - Add members with optional role
  - Remove members
  - Role assignment (admin/moderator/member)
  
- **TestChannelStructure**: Response validation
  - All required fields present
  - Member structure and nested objects

#### 4. **test_messages.py** — Messaging System (32 tests)
- **TestSendMessage**: Message creation
  - Text message creation
  - Reply-to messages
  - Message type field (text/file/image/reply)
  - Content validation
  
- **TestGetMessages**: Message retrieval
  - Get channel messages with pagination
  - `before` cursor for time-based pagination
  - `limit` parameter (1-200)
  - `has_more` flag for pagination
  
- **TestEditMessage**: Message updates
  - Edit message content
  - Edit timestamp tracking
  - Permission validation
  
- **TestDeleteMessage**: Message deletion
  - Soft delete from channel
  - Verify deletion in message list
  
- **TestMessageReactions**: Emoji reactions
  - Add reactions to messages
  - Toggle reactions (add/remove)
  - Multiple different reactions per message
  - Reaction aggregation (count, user list)
  
- **TestMarkMessageRead**: Read receipts
  - Mark message as read
  - Channel ID requirement

#### 5. **test_health.py** — System Status (8 tests)
- **TestHealthCheck**: `/health` endpoint
  - Status field verification
  - Service name and version
  - No auth required
  
- **TestServerInfo**: `/info` endpoint
  - LAN IP detection
  - Uptime tracking
  - Online user count
  - System information

#### 6. **test_security.py** — Security Primitives (53 tests)
- **TestPasswordHashing**: Bcrypt integration
  - Password hashing with unique salts
  - Verification against correct/wrong passwords
  - Hash format validation
  - Error handling for corrupted hashes
  
- **TestAccessTokens**: JWT access tokens
  - Token creation and signing
  - Payload decoding and validation
  - Required claims (sub, type, exp, iat, jti)
  - Extra claims support
  - Fingerprint binding
  
- **TestRefreshTokens**: Long-lived refresh tokens
  - Creation and validation
  - Type field enforcement
  - Longer expiry than access tokens
  
- **TestTokenExpiry**: Token expiration
  - Expired token rejection
  - IAT and EXP claim validation
  
- **TestTokenRevocation**: JTI-based blacklisting
  - Revoke tokens by JTI
  - Revoked token rejection
  - Independent JTI revocation
  - Multiple token revocation
  
- **TestTokenStructure**: JWT structure validation
  - Unique JTI per token
  - Correct algorithm (HS256)
  - Subject claim matching
  
- **TestErrorMessages**: Security error handling
  - Generic error messages (no info leakage)
  - Clear expired token messages

#### 7. **test_presence.py** — Presence Service (20 tests)
- **TestPresenceConnect**: Online status tracking
  - Connect socket marks user online
  - Multiple connections per user
  - Last seen timestamp updates
  
- **TestPresenceDisconnect**: Offline tracking
  - Last connection disconnection marks offline
  - Partial disconnection keeps online
  - Last seen updates on disconnect
  
- **TestPresenceStatus**: User presence status
  - Set status (online/away/busy/dnd)
  - Get status queries
  - Per-user status isolation
  
- **TestPresenceQueries**: Presence information
  - Get all online users
  - Get user connections
  - Get user from socket ID
  - Exclude offline users
  
- **TestPresenceCleanup**: Data management
  - Cleanup on complete disconnect
  - Status persistence across reconnections
  
- **TestPresenceLargeScale**: Performance tests
  - Handle 100+ online users
  - Handle 10+ connections per user

## Fixtures and Setup

### Global Fixtures (conftest.py)

```python
# Database and async setup
event_loop              # Session-scoped async event loop
test_engine             # In-memory SQLite async engine with tables
db_session              # Function-scoped session with automatic rollback

# HTTP client
client                  # AsyncClient for httpx with FastAPI app

# Authentication
auth_headers            # Bearer token for registered test user
second_user_headers     # Bearer token for second test user

# Test data
test_user_data          # Username, display_name, password
test_user               # Registered test user object
second_user             # Second registered user object
```

### Database Setup

Tests use in-memory SQLite (`sqlite+aiosqlite:///:memory:`) for speed and isolation:
- Tables created fresh for each test session
- Session rolled back after each test
- No external dependencies or file I/O

### Authentication Flow

Each test that requires auth:
1. Receives `auth_headers` fixture with Bearer token
2. Token created from pre-registered test user
3. Passed to API endpoints: `headers=auth_headers`

## Running Tests

### Run all tests
```bash
pytest tests/
```

### Run specific test module
```bash
pytest tests/test_auth.py
pytest tests/test_messages.py
```

### Run specific test class
```bash
pytest tests/test_auth.py::TestLogin
```

### Run specific test
```bash
pytest tests/test_auth.py::TestLogin::test_login_success
```

### Run with verbose output
```bash
pytest tests/ -v
```

### Run with print statements
```bash
pytest tests/ -s
```

### Run with coverage
```bash
pip install pytest-cov
pytest tests/ --cov=app --cov-report=html
```

### Run only async tests
```bash
pytest tests/ -k "test_" --asyncio-mode=auto
```

## Test Patterns

### Basic HTTP Test
```python
async def test_endpoint_success(self, client: AsyncClient, auth_headers: dict):
    response = await client.get("/api/endpoint", headers=auth_headers)
    
    assert response.status_code == 200
    data = response.json()
    assert data["field"] == "value"
```

### Database Test
```python
async def test_with_database(self, db_session: AsyncSession):
    user = User(username="test", ...)
    db_session.add(user)
    await db_session.commit()
    
    # Test code...
```

### Security Test
```python
def test_password_hashing(self):
    hashed = hash_password("password")
    assert verify_password("password", hashed) == True
    assert verify_password("wrong", hashed) == False
```

### Error Handling
```python
async def test_unauthorized(self, client: AsyncClient):
    response = await client.get("/api/endpoint")
    assert response.status_code == 403
```

## Test Organization

### By Feature
- **Authentication**: test_auth.py
- **Users**: test_users.py
- **Channels**: test_channels.py
- **Messages**: test_messages.py
- **System**: test_health.py

### By Layer
- **HTTP Tests**: test_auth.py, test_users.py, test_channels.py, test_messages.py, test_health.py
- **Unit Tests**: test_security.py, test_presence.py

### By Scope
- **Integration Tests**: auth flow, channel creation + messaging
- **Unit Tests**: password hashing, token operations, presence service
- **API Tests**: all endpoint tests

## Configuration

### pytest.ini
```ini
[pytest]
asyncio_mode = auto              # Auto-detect async tests
testpaths = tests                # Test directory
python_files = test_*.py         # Test file pattern
python_classes = Test*           # Test class pattern
python_functions = test_*        # Test function pattern
addopts = -v --tb=short          # Verbose output, short traceback
```

### Async Mode
All tests use `pytest-asyncio` with `asyncio_mode = "auto"`:
- No manual markers needed
- Automatic event loop handling
- Works with both async and sync tests

## Extending Tests

### Add new test file
1. Create `test_feature.py` in tests/ directory
2. Define test classes: `class TestFeature:`
3. Define test methods: `async def test_something(self, client):`
4. Use fixtures from conftest.py

### Add new fixture
1. Edit `tests/conftest.py`
2. Define with `@pytest.fixture` decorator
3. Specify scope: session, module, function
4. Use in tests as parameter

### Test new endpoint
1. Add test class to appropriate module
2. Call endpoint via `client.post/get/patch/delete()`
3. Assert status code and response fields
4. Test error cases (401, 403, 404, 422)

## Important Notes

### Database Isolation
Each test gets fresh session with automatic rollback:
- No data persists between tests
- Tests can run in any order
- No cleanup code needed

### Authentication Testing
- Use `auth_headers` fixture for authenticated requests
- Use no headers for testing 403/401 errors
- Use invalid token for testing 401 errors

### Async/Await
- All HTTP tests are async (use `async def`)
- Use `await client.post(...)` etc.
- Sync tests allowed for unit tests (test_security.py, test_presence.py)

### Error Assertions
```python
assert response.status_code == 400      # Status code
assert "error message" in response.json()["detail"]  # Error detail
```

## CI/CD Integration

To add to CI/CD pipeline:

```yaml
# GitHub Actions example
- name: Run tests
  run: |
    pip install -r requirements.txt
    pytest tests/ --cov=app --cov-report=xml

- name: Upload coverage
  uses: codecov/codecov-action@v3
```

## Troubleshooting

### Tests fail with "No module named 'app'"
- Run from project root directory
- Ensure requirements.txt installed: `pip install -r requirements.txt`

### Tests timeout
- Check for infinite loops in code
- Increase timeout: `pytest --timeout=30 tests/`

### Database locked error
- SQLite concurrency issue in tests
- Tests should not run in parallel
- Run with: `pytest tests/ -n0` (if using pytest-xdist)

### Token/Auth errors
- Ensure test_user fixture is used
- Verify auth_headers passed to endpoints
- Check JWT_SECRET is consistent

## Test Statistics

- **Total Tests**: 151
- **HTTP Tests**: 128 (async)
- **Unit Tests**: 23 (sync)
- **Test Classes**: 35
- **Test Modules**: 7
- **Fixtures**: 8
- **Lines of Code**: ~1500

## Maintenance

### Update test when API changes
1. Update test expectations
2. Update fixtures if schema changes
3. Add new tests for new endpoints
4. Ensure backward compatibility tests

### Keep tests isolated
- Don't share state between tests
- Use fixtures for setup
- Clean up in teardown

### Document complex tests
- Add docstrings explaining test purpose
- Comment non-obvious assertions
- Link to relevant code sections

## Resources

- [pytest documentation](https://docs.pytest.org/)
- [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio)
- [httpx documentation](https://www.python-httpx.org/)
- [FastAPI testing](https://fastapi.tiangolo.com/advanced/async-tests/)
- [SQLAlchemy async](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
