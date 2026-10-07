package org.goodoldphotos.androidpoc;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;

public class AndroidOAuthHandoffTest {
    @Test
    public void verifierHasExpectedBase64UrlFormatAndEntropyLength() {
        SecureRandom fixedRandom = new SecureRandom() {
            @Override
            public void nextBytes(byte[] bytes) {
                for (int index = 0; index < bytes.length; index++) {
                    bytes[index] = (byte) index;
                }
            }
        };

        String verifier = AndroidOAuthHandoff.generateVerifier(fixedRandom);

        assertEquals(43, verifier.length());
        assertTrue(verifier.matches("[A-Za-z0-9_-]{43}"));
        assertEquals(32, AndroidOAuthHandoff.decodeBase64Url(verifier).length);
    }

    @Test
    public void challengeIsBase64UrlSha256OfAsciiVerifier() throws Exception {
        String verifier = "0123456789abcdefghijklmnopqrstuv";
        String expected = AndroidOAuthHandoff.encodeBase64Url(
                MessageDigest.getInstance("SHA-256")
                        .digest(verifier.getBytes(StandardCharsets.US_ASCII)));

        String challenge = AndroidOAuthHandoff.createChallenge(verifier);

        assertEquals(expected, challenge);
        assertEquals(43, challenge.length());
        assertFalse(challenge.contains("="));
    }

    @Test
    public void pendingLoginExpiresAfterTenMinutes() {
        long createdAt = 1_000_000L;

        assertTrue(AndroidOAuthHandoff.isPendingLoginValid(createdAt, createdAt));
        assertTrue(AndroidOAuthHandoff.isPendingLoginValid(
                createdAt,
                createdAt + AndroidOAuthHandoff.PENDING_LOGIN_TIMEOUT_MS - 1));
        assertFalse(AndroidOAuthHandoff.isPendingLoginValid(
                createdAt,
                createdAt + AndroidOAuthHandoff.PENDING_LOGIN_TIMEOUT_MS));
        assertFalse(AndroidOAuthHandoff.isPendingLoginValid(createdAt, createdAt - 1));
    }

    @Test
    public void appLinkUriIsRecognizedAndOnlyItsCodeIsAccepted() {
        String code = AndroidOAuthHandoff.encodeBase64Url(new byte[32]);
        String url = "https://mygoodoldphotos.onrender.com/android/oauth/callback?code=" + code;

        assertTrue(AndroidOAuthHandoff.isCallbackUri(url));
        assertEquals(code, AndroidOAuthHandoff.extractHandoffCode(url));
        assertFalse(AndroidOAuthHandoff.isCallbackUri(
                "https://example.com/android/oauth/callback?code=" + code));
        assertNull(AndroidOAuthHandoff.extractHandoffCode(
                url + "&extra=value"));
    }

    @Test
    public void callbackWithMissingCodeIsRejected() {
        String url = "https://mygoodoldphotos.onrender.com/android/oauth/callback";

        assertTrue(AndroidOAuthHandoff.isCallbackUri(url));
        assertNull(AndroidOAuthHandoff.extractHandoffCode(url));
    }

    @Test
    public void callbackWithoutPendingVerifierIsNotAcceptable() {
        String code = AndroidOAuthHandoff.encodeBase64Url(new byte[32]);
        String url = "https://mygoodoldphotos.onrender.com/android/oauth/callback?code=" + code;

        assertFalse(AndroidOAuthHandoff.isHandoffAcceptable(url, null, System.currentTimeMillis()));
        assertNotNull(AndroidOAuthHandoff.extractHandoffCode(url));
    }

    @Test
    public void successfulRedemptionRequiresLocalSessionCookieBeforeWebViewCanReload() {
        assertTrue(AndroidOAuthHandoff.isSuccessfulRedemption(
                200,
                "{\"status\":\"ok\"}",
                "mygoodoldphotos_session=signed-value; Path=/; HttpOnly; SameSite=lax"));
    }

    @Test
    public void failedRedemptionNeverQualifiesForAuthenticatedWebViewReload() {
        assertFalse(AndroidOAuthHandoff.isSuccessfulRedemption(
                400,
                "{\"detail\":\"invalid handoff\"}",
                null));
        assertFalse(AndroidOAuthHandoff.isSuccessfulRedemption(
                200,
                "{\"status\":\"ok\"}",
                null));
        assertFalse(AndroidOAuthHandoff.isSuccessfulRedemption(
                200,
                "{\"status\":\"error\"}",
                "mygoodoldphotos_session=signed-value; Path=/; HttpOnly"));
        assertFalse(AndroidOAuthHandoff.isSuccessfulRedemption(
                200,
                "{\"status\":\"ok\"}",
                "mygoodoldphotos_session=signed-value; Path=/"));
    }
}
