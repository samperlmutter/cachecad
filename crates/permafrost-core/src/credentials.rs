//! Production uses the OS store directly. Local Mac debug builds delegate to a
//! fixed, authenticated helper so login-Keychain build-hash partitions survive
//! rebuilding the application. No credential reaches the WebView or build logs.
type Result<T> = std::result::Result<T, keyring::Error>;

pub(crate) enum Credential {
    Native(keyring::Entry),
    #[cfg(all(target_os = "macos", debug_assertions))]
    Development {
        account: String,
        path: &'static str,
    },
}

impl Credential {
    pub(crate) fn new(service: &str, account: &str) -> Result<Self> {
        #[cfg(all(target_os = "macos", debug_assertions))]
        if let Some(path) = option_env!("PERMAFROST_DEV_KEYCHAIN_HELPER") {
            use sha2::Digest;
            if service != "com.permafrost.desktop.google" {
                return Err(failed());
            }
            let expected = option_env!("PERMAFROST_DEV_KEYCHAIN_SHA256").ok_or_else(failed)?;
            let metadata = std::fs::symlink_metadata(path).map_err(|_| failed())?;
            use std::os::unix::fs::PermissionsExt;
            if !metadata.is_file()
                || metadata.len() > 16 * 1024 * 1024
                || metadata.permissions().mode() & 0o022 != 0
            {
                return Err(failed());
            }
            let bytes = std::fs::read(path).map_err(|_| failed())?;
            if format!("{:x}", sha2::Sha256::digest(bytes)) != expected {
                return Err(failed());
            }
            return Ok(Self::Development {
                account: account.into(),
                path,
            });
        }
        keyring::Entry::new(service, account).map(Self::Native)
    }
    pub(crate) fn get_password(&self) -> Result<String> {
        match self {
            Self::Native(entry) => entry.get_password(),
            #[cfg(all(target_os = "macos", debug_assertions))]
            Self::Development { account, path } => {
                request(path, account, "get", None)?.ok_or_else(failed)
            }
        }
    }
    pub(crate) fn set_password(&self, value: &str) -> Result<()> {
        match self {
            Self::Native(entry) => entry.set_password(value),
            #[cfg(all(target_os = "macos", debug_assertions))]
            Self::Development { account, path } => {
                request(path, account, "set", Some(value)).map(|_| ())
            }
        }
    }
    pub(crate) fn delete_credential(&self) -> Result<()> {
        match self {
            Self::Native(entry) => entry.delete_credential(),
            #[cfg(all(target_os = "macos", debug_assertions))]
            Self::Development { account, path } => {
                request(path, account, "delete", None).map(|_| ())
            }
        }
    }
}

#[cfg(all(target_os = "macos", debug_assertions))]
fn failed() -> keyring::Error {
    keyring::Error::PlatformFailure(Box::new(std::io::Error::other(
        "The development credential helper could not complete the request.",
    )))
}

#[cfg(all(target_os = "macos", debug_assertions))]
fn request(
    path: &str,
    account: &str,
    operation: &str,
    value: Option<&str>,
) -> Result<Option<String>> {
    use std::{
        io::{Read, Write},
        process::{Command, Stdio},
    };
    #[derive(serde::Deserialize)]
    struct Response {
        status: i32,
        value: Option<String>,
    }
    let input = serde_json::to_vec(
        &serde_json::json!({ "operation": operation, "account": account, "value": value }),
    )
    .map_err(|_| failed())?;
    if input.len() > 65536 {
        return Err(failed());
    }
    let mut child = Command::new(path)
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| failed())?;
    let mut ready = [0; 4];
    let authenticated = child
        .stdout
        .as_mut()
        .ok_or_else(failed)?
        .read_exact(&mut ready)
        .is_ok()
        && ready == *b"PF1\n"
        && option_env!("PERMAFROST_DEV_KEYCHAIN_CDHASH")
            .is_some_and(|hash| authenticate_process(child.id(), hash));
    if !authenticated {
        let _ = child.kill();
        let _ = child.wait();
        return Err(failed());
    }
    let written = child.stdin.take().ok_or_else(failed)?.write_all(&input);
    if written.is_err() {
        let _ = child.kill();
        let _ = child.wait();
        return Err(failed());
    }
    let output = child.wait_with_output().map_err(|_| failed())?;
    if !output.status.success() || output.stdout.len() > 131072 {
        return Err(failed());
    }
    let response: Response = serde_json::from_slice(&output.stdout).map_err(|_| failed())?;
    match response.status {
        0 => Ok(response.value),
        -25300 => Err(keyring::Error::NoEntry),
        _ => Err(failed()),
    }
}

#[cfg(all(target_os = "macos", debug_assertions))]
fn authenticate_process(pid: u32, cdhash: &str) -> bool {
    use security_framework::os::macos::code_signing::{
        Flags, GuestAttributes, SecCode, SecRequirement,
    };
    if cdhash.len() != 40 || !cdhash.bytes().all(|c| c.is_ascii_hexdigit()) {
        return false;
    }
    let expression = format!(
        "identifier \"com.permafrost.desktop.development-keychain\" and cdhash H\"{cdhash}\""
    );
    let Ok(requirement) = expression.parse::<SecRequirement>() else {
        return false;
    };
    let mut attributes = GuestAttributes::new();
    attributes.set_pid(pid as i32);
    SecCode::copy_guest_with_attribues(None, &attributes, Flags::NONE)
        .and_then(|code| code.check_validity(Flags::STRICT_VALIDATE, &requirement))
        .is_ok()
}

#[cfg(all(test, target_os = "macos", debug_assertions))]
mod tests {
    use super::authenticate_process;
    use std::{
        io::Read,
        process::{Command, Stdio},
    };

    #[test]
    fn a_running_helper_must_match_its_pinned_code_hash() {
        let root = tempfile::tempdir().unwrap();
        let source = root.path().join("helper.c");
        let executable = root.path().join("helper with spaces");
        std::fs::write(&source, b"#include <unistd.h>\nint main(void) { write(1, \"PF1\\n\", 4); char c; return read(0, &c, 1) < 0; }\n").unwrap();
        assert!(Command::new("cc")
            .arg(&source)
            .arg("-o")
            .arg(&executable)
            .output()
            .unwrap()
            .status
            .success());
        assert!(Command::new("/usr/bin/codesign")
            .args([
                "--force",
                "--sign",
                "-",
                "--identifier",
                "com.permafrost.desktop.development-keychain"
            ])
            .arg(&executable)
            .output()
            .unwrap()
            .status
            .success());
        let signing = Command::new("/usr/bin/codesign")
            .args(["-d", "--verbose=4"])
            .arg(&executable)
            .output()
            .unwrap();
        assert!(signing.status.success());
        let information = String::from_utf8(signing.stderr).unwrap();
        let hash = information
            .lines()
            .find_map(|line| line.strip_prefix("CDHash="))
            .unwrap();
        let mut child = Command::new(&executable)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut ready = [0; 4];
        let started = child
            .stdout
            .as_mut()
            .unwrap()
            .read_exact(&mut ready)
            .is_ok();
        let accepted = authenticate_process(child.id(), hash);
        let wrong_hash = authenticate_process(child.id(), &"0".repeat(40));
        let malformed = authenticate_process(child.id(), "not a code requirement");
        let _ = child.kill();
        child.wait().unwrap();
        assert!(started && ready == *b"PF1\n");
        assert!(accepted, "The live helper's actual code hash was rejected");
        assert!(!wrong_hash && !malformed, "An unpinned process was trusted");
    }
}
