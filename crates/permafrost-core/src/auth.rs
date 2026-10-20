//! Installed-app authorization. Tokens never cross the desktop IPC boundary.
use crate::{model::now, Error, Result};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::time::Duration;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
};
use tokio_util::sync::CancellationToken;
use url::Url;

const SCOPES: &str = "openid email profile https://www.googleapis.com/auth/drive";
const STORE_SERVICE: &str = "com.permafrost.desktop.google";

#[derive(Clone)]
pub struct OAuthConfig {
    pub client_id: String,
    pub client_secret: String,
}
impl OAuthConfig {
    pub fn bundled() -> Option<Self> {
        let id = option_env!("PERMAFROST_GOOGLE_CLIENT_ID").unwrap_or("");
        let secret = option_env!("PERMAFROST_GOOGLE_CLIENT_SECRET").unwrap_or("");
        if id.is_empty() {
            None
        } else {
            Some(Self {
                client_id: id.into(),
                client_secret: secret.into(),
            })
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Account {
    pub name: String,
    pub email: String,
    pub subject: String,
}
#[derive(Serialize, Deserialize)]
struct SavedSession {
    client_id: String,
    refresh_token: String,
    account: Account,
}
#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
    refresh_token: Option<String>,
    expires_in: u64,
    scope: Option<String>,
}
#[derive(Deserialize)]
struct UserInfo {
    sub: String,
    email: String,
    email_verified: bool,
    name: Option<String>,
}

pub struct Auth {
    config: Option<OAuthConfig>,
    client: reqwest::Client,
    session: Option<SavedSession>,
    access: Option<(String, u64)>,
}
impl Auth {
    pub fn new(config: Option<OAuthConfig>) -> Result<Self> {
        Ok(Self {
            config,
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(30))
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
            session: None,
            access: None,
        })
    }
    pub fn configured(&self) -> bool {
        self.config.is_some()
    }
    pub fn account(&self) -> Option<Account> {
        self.session.as_ref().map(|s| s.account.clone())
    }
    fn key(&self) -> Result<crate::credentials::Credential> {
        let config = self.config.as_ref().ok_or(Error::SignedOut)?;
        crate::credentials::Credential::new(STORE_SERVICE, &config.client_id)
            .map_err(|_| Error::CredentialStore)
    }
    pub fn restore(&mut self) -> Result<Option<Account>> {
        if self.config.is_none() {
            return Ok(None);
        }
        let saved = match self.key()?.get_password() {
            Ok(s) => s,
            Err(keyring::Error::NoEntry) => return Ok(None),
            Err(_) => return Err(Error::CredentialStore),
        };
        let session: SavedSession =
            serde_json::from_str(&saved).map_err(|_| Error::CredentialStore)?;
        if Some(session.client_id.as_str()) != self.config.as_ref().map(|c| c.client_id.as_str())
            || session.refresh_token.is_empty()
            || session.account.subject.is_empty()
        {
            return Err(Error::CredentialStore);
        }
        self.session = Some(session);
        Ok(self.account())
    }
    pub async fn access_token(&mut self) -> Result<String> {
        if let Some((token, expiry)) = &self.access {
            if *expiry > now() + 60 {
                return Ok(token.clone());
            }
        }
        let config = self.config.as_ref().ok_or(Error::SignedOut)?;
        let session = self.session.as_ref().ok_or(Error::SignedOut)?;
        let response = self
            .client
            .post("https://oauth2.googleapis.com/token")
            .form(&[
                ("client_id", config.client_id.as_str()),
                ("client_secret", &config.client_secret),
                ("refresh_token", &session.refresh_token),
                ("grant_type", "refresh_token"),
            ])
            .send()
            .await?;
        if response.status().as_u16() == 400 || response.status().as_u16() == 401 {
            self.access = None;
            return Err(Error::SignedOut);
        }
        let token: TokenResponse = checked(response)?.json().await?;
        if token.access_token.is_empty() {
            return Err(Error::SignedOut);
        }
        if let Some(refresh) = token.refresh_token {
            let session = self.session.as_mut().ok_or(Error::SignedOut)?;
            session.refresh_token = refresh;
            self.key()?
                .set_password(&serde_json::to_string(self.session.as_ref().unwrap())?)
                .map_err(|_| Error::CredentialStore)?;
        }
        self.access = Some((token.access_token.clone(), now() + token.expires_in));
        Ok(token.access_token)
    }
    pub async fn sign_in(&mut self, cancel: &CancellationToken) -> Result<Account> {
        let config = self.config.as_ref().ok_or_else(|| Error::Invalid("Google sign-in is not configured in this build. The distributor must supply its Desktop OAuth client.".into()))?;
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let redirect = format!(
            "http://127.0.0.1:{}/callback",
            listener.local_addr()?.port()
        );
        let verifier = random_url_token();
        let state = random_url_token();
        let mut url = Url::parse("https://accounts.google.com/o/oauth2/v2/auth").unwrap();
        url.query_pairs_mut().extend_pairs([
            ("client_id", config.client_id.as_str()),
            ("redirect_uri", &redirect),
            ("response_type", "code"),
            ("scope", SCOPES),
            ("access_type", "offline"),
            ("prompt", "consent"),
            ("state", &state),
            (
                "code_challenge",
                &URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes())),
            ),
            ("code_challenge_method", "S256"),
        ]);
        open::that_detached(url.as_str()).map_err(|_| {
            Error::Invalid("Could not open your browser for Google sign-in.".into())
        })?;
        let code = tokio::select! {
            _ = cancel.cancelled() => return Err(Error::Cancelled),
            result = tokio::time::timeout(Duration::from_secs(600), callback(&listener, &state)) => result.map_err(|_| Error::Invalid("Google sign-in timed out. Try again.".into()))??,
        };
        let response = self
            .client
            .post("https://oauth2.googleapis.com/token")
            .form(&[
                ("client_id", config.client_id.as_str()),
                ("client_secret", &config.client_secret),
                ("code", &code),
                ("code_verifier", &verifier),
                ("redirect_uri", &redirect),
                ("grant_type", "authorization_code"),
            ])
            .send()
            .await?;
        let token: TokenResponse = checked(response)?.json().await?;
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        if !token.scope.as_ref().is_some_and(|s| {
            s.split_whitespace()
                .any(|s| s == "https://www.googleapis.com/auth/drive")
        }) {
            return Err(Error::Invalid(
                "Google Drive permission is required to sync an existing project folder.".into(),
            ));
        }
        // The identity is obtained from Google's authenticated HTTPS userinfo
        // endpoint for this access token. Unverified ID-token payloads are unused.
        let user: UserInfo = checked(
            self.client
                .get("https://openidconnect.googleapis.com/v1/userinfo")
                .bearer_auth(&token.access_token)
                .send()
                .await?,
        )?
        .json()
        .await?;
        if user.sub.is_empty() || !user.email_verified {
            return Err(Error::SignedOut);
        }
        let account = Account {
            name: user.name.unwrap_or_else(|| user.email.clone()),
            email: user.email,
            subject: user.sub,
        };
        let session = SavedSession {
            client_id: config.client_id.clone(),
            refresh_token: token
                .refresh_token
                .filter(|s| !s.is_empty())
                .ok_or(Error::SignedOut)?,
            account: account.clone(),
        };
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        self.key()?
            .set_password(&serde_json::to_string(&session)?)
            .map_err(|_| Error::CredentialStore)?;
        self.session = Some(session);
        self.access = Some((token.access_token, now() + token.expires_in));
        Ok(account)
    }
    pub fn sign_out(&mut self) -> Result<()> {
        match self.key()?.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => {}
            Err(_) => return Err(Error::CredentialStore),
        }
        self.access = None;
        self.session = None;
        Ok(())
    }
    pub async fn disconnect(&mut self) -> Result<()> {
        let session = self.session.as_ref().ok_or(Error::SignedOut)?;
        checked(
            self.client
                .post("https://oauth2.googleapis.com/revoke")
                .form(&[("token", &session.refresh_token)])
                .send()
                .await?,
        )?;
        self.sign_out()
    }
}

fn random_url_token() -> String {
    let mut bytes = [0u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    URL_SAFE_NO_PAD.encode(bytes)
}
pub(crate) fn checked(response: reqwest::Response) -> Result<reqwest::Response> {
    if response.status().is_success() {
        Ok(response)
    } else if response.status().as_u16() == 401 {
        Err(Error::SignedOut)
    } else {
        Err(Error::Provider(response.status().as_u16()))
    }
}
fn parse_callback(request: &str, state: &str) -> Result<String> {
    let line = request.lines().next().unwrap_or("");
    let parts: Vec<_> = line.split_whitespace().collect();
    if parts.len() != 3 || parts[0] != "GET" || !parts[2].starts_with("HTTP/1.") {
        return Err(Error::Invalid("Invalid sign-in callback.".into()));
    }
    let url = Url::parse(&format!("http://127.0.0.1{}", parts[1]))
        .map_err(|_| Error::Invalid("Invalid sign-in callback.".into()))?;
    let params: Vec<_> = url.query_pairs().collect();
    let states: Vec<_> = params.iter().filter(|(k, _)| k == "state").collect();
    let codes: Vec<_> = params.iter().filter(|(k, _)| k == "code").collect();
    if url.path() != "/callback"
        || states.len() != 1
        || states[0].1 != state
        || codes.len() != 1
        || codes[0].1.is_empty()
        || params.iter().any(|(k, _)| k == "error")
    {
        return Err(Error::Invalid(
            "Google sign-in was declined or its callback was invalid.".into(),
        ));
    }
    Ok(codes[0].1.to_string())
}
async fn callback(listener: &TcpListener, state: &str) -> Result<String> {
    loop {
        let (mut socket, _) = listener.accept().await?;
        let mut request = Vec::new();
        let read = tokio::time::timeout(Duration::from_secs(5), async {
            let mut chunk = [0; 1024];
            while request.len() < 8192 {
                let n = socket.read(&mut chunk).await?;
                if n == 0 {
                    break;
                }
                request.extend_from_slice(&chunk[..n]);
                if request.windows(4).any(|w| w == b"\r\n\r\n") {
                    break;
                }
            }
            std::io::Result::Ok(())
        })
        .await;
        if !matches!(read, Ok(Ok(()))) {
            continue;
        }
        let parsed = parse_callback(&String::from_utf8_lossy(&request), state);
        let body = if parsed.is_ok() {
            "Sign-in complete. You can return to Permafrost."
        } else {
            "This sign-in callback was not accepted. Return to Permafrost and try again."
        };
        let response = format!("HTTP/1.1 {}\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}", if parsed.is_ok() { "200 OK" } else { "400 Bad Request" }, body.len(), body);
        let _ = socket.write_all(response.as_bytes()).await;
        if let Ok(code) = parsed {
            return Ok(code);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn callback_checks_path_state_and_duplicate_values() {
        assert_eq!(
            parse_callback("GET /callback?state=s&code=ok HTTP/1.1\r\n", "s").unwrap(),
            "ok"
        );
        for request in [
            "GET /callback?state=wrong&code=ok HTTP/1.1",
            "GET /other?state=s&code=ok HTTP/1.1",
            "GET /callback?state=s&state=s&code=ok HTTP/1.1",
            "GET /callback?state=s&code=ok&code=other HTTP/1.1",
            "POST /callback?state=s&code=ok HTTP/1.1",
            "GET /callback?state=s&error=denied HTTP/1.1",
        ] {
            assert!(parse_callback(request, "s").is_err());
        }
    }
}
