//! The published Android build manifest: what it must contain, whether it
//! describes a newer build than the installed one, and how a downloaded APK is
//! proven to match it.
//!
//! The Android release workflow publishes `android-latest.json` beside the
//! rolling APK. The rules live here, free of device APIs, so they run under
//! `cargo test` on any host; `update_android` adds the manifest location, the
//! network and the installer half behind `target_os = "android"`.

use serde::Deserialize;

const MAX_VERSION_NAME_BYTES: usize = 64;
const SHA256_HEX_LEN: usize = 64;

/// The parts of `android-latest.json` the client acts on. Unknown fields are
/// ignored so the release workflow can add diagnostics without a client change.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Manifest {
    pub version_code: u64,
    pub version_name: String,
    pub apk_url: String,
    pub sha256: String,
}

#[derive(Debug, PartialEq, Eq)]
pub enum ManifestError {
    Json,
    VersionCode,
    VersionName,
    ApkUrl,
    Sha256,
}

impl std::fmt::Display for ManifestError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let message = match self {
            Self::Json => "the manifest is not valid JSON",
            Self::VersionCode => "the manifest has no positive versionCode",
            Self::VersionName => "the manifest has no usable versionName",
            Self::ApkUrl => "the manifest apkUrl is not an https github.com URL",
            Self::Sha256 => "the manifest sha256 is not a hex SHA-256 digest",
        };
        formatter.write_str(message)
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawManifest {
    version_code: u64,
    version_name: String,
    apk_url: String,
    sha256: String,
}

/// Validate a manifest before anything trusts its build number or URL.
///
/// Every rejection is a reason to fall back to the download page, never to
/// install something the check could not verify.
pub fn parse_manifest(json: &str) -> Result<Manifest, ManifestError> {
    let raw: RawManifest = serde_json::from_str(json).map_err(|_| ManifestError::Json)?;
    if raw.version_code == 0 {
        return Err(ManifestError::VersionCode);
    }
    let version_name = raw.version_name.trim();
    if version_name.is_empty() || version_name.len() > MAX_VERSION_NAME_BYTES {
        return Err(ManifestError::VersionName);
    }
    let apk_url = reqwest::Url::parse(raw.apk_url.trim()).map_err(|_| ManifestError::ApkUrl)?;
    if apk_url.scheme() != "https"
        || apk_url.host_str() != Some("github.com")
        || !apk_url.username().is_empty()
        || apk_url.password().is_some()
    {
        return Err(ManifestError::ApkUrl);
    }
    let sha256 = raw.sha256.trim().to_ascii_lowercase();
    if sha256.len() != SHA256_HEX_LEN || !sha256.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(ManifestError::Sha256);
    }
    Ok(Manifest {
        version_code: raw.version_code,
        version_name: version_name.to_string(),
        apk_url: apk_url.to_string(),
        sha256,
    })
}

/// The build number is the ordering: versionName is a human milestone and may
/// repeat across builds, while the release workflow increments the code.
pub fn is_newer(manifest: &Manifest, installed_version_code: u64) -> bool {
    manifest.version_code > installed_version_code
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

pub fn digest_matches(expected: &str, actual: &str) -> bool {
    expected.eq_ignore_ascii_case(actual)
}

#[cfg(test)]
mod tests {
    use sha2::{Digest, Sha256};

    use super::{digest_matches, hex, is_newer, parse_manifest, ManifestError};

    fn sha256_hex(bytes: &[u8]) -> String {
        hex(Sha256::digest(bytes).as_slice())
    }

    const DIGEST: &str = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

    fn manifest(version_code: u64, apk_url: &str, sha256: &str) -> String {
        format!(
            r#"{{"versionCode":{version_code},"versionName":"1.2.3","apkUrl":"{apk_url}","sha256":"{sha256}","commit":"abc","builtAt":"2026-10-17T00:00:00Z"}}"#
        )
    }

    fn valid() -> String {
        manifest(
            4003,
            "https://github.com/zhiyuan-zhang0206/Ava/releases/download/android-latest/ava-app-1.2.3.apk",
            DIGEST,
        )
    }

    #[test]
    fn parses_the_published_manifest_shape() {
        let parsed = parse_manifest(&valid()).expect("a published manifest must parse");
        assert_eq!(parsed.version_code, 4003);
        assert_eq!(parsed.version_name, "1.2.3");
        assert_eq!(parsed.sha256, DIGEST);
        assert!(parsed.apk_url.starts_with("https://github.com/"));
    }

    #[test]
    fn rejects_a_document_missing_required_fields() {
        assert_eq!(
            parse_manifest(r#"{"versionCode":4003}"#),
            Err(ManifestError::Json)
        );
        assert_eq!(parse_manifest(""), Err(ManifestError::Json));
    }

    #[test]
    fn rejects_a_build_number_the_client_cannot_order() {
        let zero = manifest(0, "https://github.com/x/y.apk", DIGEST);
        assert_eq!(parse_manifest(&zero), Err(ManifestError::VersionCode));
        let negative = r#"{"versionCode":-1,"versionName":"1","apkUrl":"https://github.com/x/y.apk","sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}"#;
        assert_eq!(parse_manifest(negative), Err(ManifestError::Json));
    }

    #[test]
    fn rejects_a_version_name_that_cannot_be_displayed() {
        let blank = r#"{"versionCode":4003,"versionName":"  ","apkUrl":"https://github.com/x/y.apk","sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}"#;
        assert_eq!(parse_manifest(blank), Err(ManifestError::VersionName));
        let long = format!(
            r#"{{"versionCode":4003,"versionName":"{}","apkUrl":"https://github.com/x/y.apk","sha256":"{DIGEST}"}}"#,
            "9".repeat(65)
        );
        assert_eq!(parse_manifest(&long), Err(ManifestError::VersionName));
    }

    #[test]
    fn refuses_to_fetch_the_apk_from_anywhere_but_https_github() {
        for url in [
            "http://github.com/x/y.apk",
            "https://example.com/x/y.apk",
            "https://user:pass@github.com/x/y.apk",
            "not a url",
        ] {
            assert_eq!(
                parse_manifest(&manifest(4003, url, DIGEST)),
                Err(ManifestError::ApkUrl),
                "{url} must not be accepted"
            );
        }
    }

    #[test]
    fn refuses_a_digest_that_cannot_be_compared() {
        let repeated = "z".repeat(64);
        for digest in ["", "abc", repeated.as_str()] {
            assert_eq!(
                parse_manifest(&manifest(4003, "https://github.com/x/y.apk", digest)),
                Err(ManifestError::Sha256),
                "{digest:?} must not be accepted"
            );
        }
    }

    #[test]
    fn normalises_the_published_digest_case() {
        let upper = manifest(4003, "https://github.com/x/y.apk", &DIGEST.to_uppercase());
        assert_eq!(
            parse_manifest(&upper).expect("uppercase hex").sha256,
            DIGEST
        );
    }

    #[test]
    fn a_build_is_newer_only_above_the_installed_code() {
        let parsed = parse_manifest(&valid()).expect("manifest");
        assert!(is_newer(&parsed, 4002));
        assert!(!is_newer(&parsed, 4003));
        assert!(!is_newer(&parsed, 4004));
    }

    #[test]
    fn hashes_match_published_sha256_vectors() {
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(sha256_hex(b"abc"), DIGEST);
    }

    #[test]
    fn digest_comparison_is_case_insensitive_and_exact() {
        assert!(digest_matches(DIGEST, &DIGEST.to_uppercase()));
        assert!(!digest_matches(DIGEST, &DIGEST[1..]));
        assert!(!digest_matches(DIGEST, &sha256_hex(b"abcd")));
    }
}
