package com.example.users;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.HashSet;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * Strict cursor-pagination contract. Pins shape and cursor encoding tightly
 * so a naive first implementation that re-uses the offset-pagination shape
 * (e.g. retains "offset"/"total" keys, or returns nextCursor="" on the last
 * page, or encodes the cursor as a plain integer) will fail. Fixer repairs
 * the implementation on a later verify iteration and the run converges.
 */
@SpringBootTest
@AutoConfigureMockMvc
class CursorContractTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    private static final Set<String> ALLOWED_KEYS = Set.of("items", "nextCursor", "limit");

    @Autowired
    MockMvc mockMvc;

    @Test
    void firstPageReturnsOnlyAllowedKeysAndOpaqueCursor() throws Exception {
        MvcResult result = mockMvc.perform(get("/api/users").param("cursor", "").param("limit", "5"))
            .andExpect(status().isOk())
            .andReturn();
        JsonNode body = MAPPER.readTree(result.getResponse().getContentAsString());

        Set<String> actualKeys = new HashSet<>();
        body.fieldNames().forEachRemaining(actualKeys::add);
        assertEquals(ALLOWED_KEYS, actualKeys,
            "response must contain exactly items/nextCursor/limit; got " + actualKeys);

        assertEquals(5, body.get("items").size());
        assertEquals(5, body.get("limit").asInt());

        JsonNode cursor = body.get("nextCursor");
        assertNotNull(cursor);
        assertTrue(cursor.isTextual(), "nextCursor must be a string when more pages remain");
        String decoded = new String(Base64.getDecoder().decode(cursor.asText()), StandardCharsets.UTF_8);
        assertEquals("id:5", decoded, "cursor must encode 'id:<lastIdInPage>'");
    }

    @Test
    void cursorParamReturnsNextPage() throws Exception {
        String cursor = Base64.getEncoder().encodeToString("id:5".getBytes(StandardCharsets.UTF_8));
        MvcResult result = mockMvc.perform(get("/api/users").param("cursor", cursor).param("limit", "5"))
            .andExpect(status().isOk())
            .andReturn();
        JsonNode body = MAPPER.readTree(result.getResponse().getContentAsString());

        assertEquals(5, body.get("items").size());
        assertEquals(6, body.get("items").get(0).get("id").asLong());
    }

    @Test
    void lastPageReturnsJsonNullCursor() throws Exception {
        String cursor = Base64.getEncoder().encodeToString("id:45".getBytes(StandardCharsets.UTF_8));
        MvcResult result = mockMvc.perform(get("/api/users").param("cursor", cursor).param("limit", "10"))
            .andExpect(status().isOk())
            .andReturn();
        JsonNode body = MAPPER.readTree(result.getResponse().getContentAsString());

        JsonNode nextCursor = body.get("nextCursor");
        assertNotNull(nextCursor, "nextCursor field must be present even on the last page");
        assertTrue(nextCursor.isNull(),
            "nextCursor must be JSON null on the last page, not an empty string or absent key");
    }
}
