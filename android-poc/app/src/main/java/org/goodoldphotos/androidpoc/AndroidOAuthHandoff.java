package org.goodoldphotos.androidpoc;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;

import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.Key;
import java.security.KeyStore;
import java.security.SecureRandom;
import java.util.regex.Pattern;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

final class AndroidOAuthHandoff {
    static final long PENDING_LOGIN_TIMEOUT_MS = 10 * 60 * 1000L;

    private static final String PREFERENCES = "android_oauth_pending";
    private static final String KEY_ALIAS = "goodoldphotos_android_oauth_verifier";
    private static final String CREATED_AT = "created_at";
    private static final String IV = "verifier_iv";
    private static final String CIPHERTEXT = "verifier_ciphertext";
    private static final Pattern BASE64URL_32_BYTES = Pattern.compile("[A-Za-z0-9_-]{43}");
    private static final Pattern SESSION_COOKIE = Pattern.compile(
            "(?i)(?:^|;\\s*)mygoodoldphotos_session=[^;\\s]+");
    private static final Pattern HTTP_ONLY_COOKIE_FLAG = Pattern.compile(
            "(?i)(?:^|;\\s*)httponly(?:;|$)");
    private static final Pattern SUCCESS_STATUS = Pattern.compile(
            "\"status\"\\s*:\\s*\"ok\"");
    private static final char[] BASE64URL_ALPHABET =
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".toCharArray();

    private AndroidOAuthHandoff() {}

    static String generateVerifier(SecureRandom random) {
        byte[] randomBytes = new byte[32];
        random.nextBytes(randomBytes);
        return encodeBase64Url(randomBytes);
    }

    static String createChallenge(String verifier) throws GeneralSecurityException {
        byte[] digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(verifier.getBytes(StandardCharsets.US_ASCII));
        return encodeBase64Url(digest);
    }

    static boolean isPendingLoginValid(long createdAt, long now) {
        return createdAt <= now && now - createdAt < PENDING_LOGIN_TIMEOUT_MS;
    }

    static String extractHandoffCode(String url) {
        if (url == null) {
            return null;
        }
        try {
            URI uri = URI.create(url);
            if (!"https".equals(uri.getScheme())
                    || !"mygoodoldphotos.onrender.com".equals(uri.getHost())
                    || uri.getPort() != -1
                    || uri.getUserInfo() != null
                    || !"/android/oauth/callback".equals(uri.getPath())
                    || uri.getFragment() != null
                    || uri.getRawQuery() == null
                    || !uri.getRawQuery().startsWith("code=")
                    || uri.getRawQuery().indexOf('&') >= 0
                    || uri.getRawQuery().indexOf(';') >= 0) {
                return null;
            }
            String code = uri.getRawQuery().substring("code=".length());
            if (!BASE64URL_32_BYTES.matcher(code).matches()) {
                return null;
            }
            byte[] decoded = decodeBase64Url(code);
            return decoded.length == 32 ? code : null;
        } catch (IllegalArgumentException exception) {
            return null;
        }
    }

    static boolean isCallbackUri(String url) {
        if (url == null) {
            return false;
        }
        try {
            URI uri = URI.create(url);
            return "https".equals(uri.getScheme())
                    && "mygoodoldphotos.onrender.com".equals(uri.getHost())
                    && uri.getPort() == -1
                    && uri.getUserInfo() == null
                    && "/android/oauth/callback".equals(uri.getPath())
                    && uri.getFragment() == null;
        } catch (IllegalArgumentException exception) {
            return false;
        }
    }

    static boolean isHandoffAcceptable(String url, PendingLogin pendingLogin, long now) {
        return extractHandoffCode(url) != null
                && pendingLogin != null
                && isPendingLoginValid(pendingLogin.createdAt, now);
    }

    static boolean isSuccessfulRedemption(int statusCode, String responseBody, String setCookie) {
        return statusCode == 200
                && responseBody != null
                && SUCCESS_STATUS.matcher(responseBody).find()
                && setCookie != null
                && SESSION_COOKIE.matcher(setCookie).find()
                && HTTP_ONLY_COOKIE_FLAG.matcher(setCookie).find();
    }

    static PendingLogin createAndStore(Context context, long now, SecureRandom random)
            throws GeneralSecurityException {
        PendingLogin previous = load(context, now);
        if (previous != null) {
            throw new IllegalStateException("An Android login is already pending.");
        }

        String verifier = generateVerifier(random);
        String challenge = createChallenge(verifier);
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey());
        byte[] encrypted = cipher.doFinal(verifier.getBytes(StandardCharsets.US_ASCII));

        SharedPreferences preferences = preferences(context);
        boolean stored = preferences.edit()
                .putLong(CREATED_AT, now)
                .putString(IV, encodeBase64Url(cipher.getIV()))
                .putString(CIPHERTEXT, encodeBase64Url(encrypted))
                .commit();
        if (!stored) {
            throw new IllegalStateException("Unable to store the pending Android login.");
        }
        return new PendingLogin(verifier, now, challenge);
    }

    static PendingLogin load(Context context, long now) throws GeneralSecurityException {
        SharedPreferences preferences = preferences(context);
        if (!preferences.contains(CREATED_AT)
                || !preferences.contains(IV)
                || !preferences.contains(CIPHERTEXT)) {
            return null;
        }

        long createdAt = preferences.getLong(CREATED_AT, 0);
        if (!isPendingLoginValid(createdAt, now)) {
            clear(context);
            return null;
        }

        try {
            byte[] iv = decodeBase64Url(preferences.getString(IV, ""));
            byte[] encrypted = decodeBase64Url(preferences.getString(CIPHERTEXT, ""));
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), new GCMParameterSpec(128, iv));
            String verifier = new String(cipher.doFinal(encrypted), StandardCharsets.US_ASCII);
            return new PendingLogin(verifier, createdAt, createChallenge(verifier));
        } catch (IllegalArgumentException exception) {
            clear(context);
            throw new GeneralSecurityException("Pending Android OAuth data is invalid.", exception);
        }
    }

    static void clear(Context context) {
        if (!preferences(context).edit().clear().commit()) {
            throw new IllegalStateException("Unable to clear the pending Android login.");
        }
    }

    private static SharedPreferences preferences(Context context) {
        return context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE);
    }

    static String encodeBase64Url(byte[] bytes) {
        StringBuilder encoded = new StringBuilder((bytes.length * 4 + 2) / 3);
        int index = 0;
        while (index + 3 <= bytes.length) {
            int value = ((bytes[index] & 0xff) << 16)
                    | ((bytes[index + 1] & 0xff) << 8)
                    | (bytes[index + 2] & 0xff);
            encoded.append(BASE64URL_ALPHABET[(value >>> 18) & 0x3f]);
            encoded.append(BASE64URL_ALPHABET[(value >>> 12) & 0x3f]);
            encoded.append(BASE64URL_ALPHABET[(value >>> 6) & 0x3f]);
            encoded.append(BASE64URL_ALPHABET[value & 0x3f]);
            index += 3;
        }

        int remaining = bytes.length - index;
        if (remaining == 1) {
            int value = (bytes[index] & 0xff) << 16;
            encoded.append(BASE64URL_ALPHABET[(value >>> 18) & 0x3f]);
            encoded.append(BASE64URL_ALPHABET[(value >>> 12) & 0x3f]);
        } else if (remaining == 2) {
            int value = ((bytes[index] & 0xff) << 16)
                    | ((bytes[index + 1] & 0xff) << 8);
            encoded.append(BASE64URL_ALPHABET[(value >>> 18) & 0x3f]);
            encoded.append(BASE64URL_ALPHABET[(value >>> 12) & 0x3f]);
            encoded.append(BASE64URL_ALPHABET[(value >>> 6) & 0x3f]);
        }
        return encoded.toString();
    }

    static byte[] decodeBase64Url(String value) {
        if (value.length() % 4 == 1) {
            throw new IllegalArgumentException("Invalid base64url value.");
        }
        ByteArrayOutputStream decoded = new ByteArrayOutputStream(value.length() * 3 / 4);
        int accumulator = 0;
        int bits = 0;
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            int digit = -1;
            for (int alphabetIndex = 0; alphabetIndex < BASE64URL_ALPHABET.length; alphabetIndex++) {
                if (BASE64URL_ALPHABET[alphabetIndex] == character) {
                    digit = alphabetIndex;
                    break;
                }
            }
            if (digit < 0) {
                throw new IllegalArgumentException("Invalid base64url value.");
            }
            accumulator = (accumulator << 6) | digit;
            bits += 6;
            if (bits >= 8) {
                bits -= 8;
                decoded.write((accumulator >>> bits) & 0xff);
            }
        }
        if (bits > 0 && (accumulator & ((1 << bits) - 1)) != 0) {
            throw new IllegalArgumentException("Non-canonical base64url value.");
        }
        return decoded.toByteArray();
    }

    private static Key getOrCreateKey() throws GeneralSecurityException {
        KeyStore keyStore = KeyStore.getInstance("AndroidKeyStore");
        try {
            keyStore.load(null);
        } catch (java.io.IOException exception) {
            throw new GeneralSecurityException("Unable to access Android Keystore.", exception);
        }
        Key key = keyStore.getKey(KEY_ALIAS, null);
        if (key != null) {
            return key;
        }

        KeyGenerator generator = KeyGenerator.getInstance(
                KeyProperties.KEY_ALGORITHM_AES,
                "AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build());
        SecretKey generated = generator.generateKey();
        if (generated == null) {
            throw new GeneralSecurityException("Unable to create Android Keystore key.");
        }
        return generated;
    }

    static final class PendingLogin {
        final String verifier;
        final long createdAt;
        final String challenge;

        PendingLogin(String verifier, long createdAt, String challenge) {
            this.verifier = verifier;
            this.createdAt = createdAt;
            this.challenge = challenge;
        }
    }
}
