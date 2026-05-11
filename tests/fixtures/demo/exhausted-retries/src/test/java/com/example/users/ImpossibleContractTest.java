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

import static org.junit.jupiter.api.Assertions.assertAll;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * Deliberately unsatisfiable contract used to demo the exhausted-retries
 * terminal status. The two assertions below are mutually exclusive: a cursor
 * cannot simultaneously be a base64 string and a plain decimal integer. No
 * implementation can satisfy both, so Fixer cannot reconcile the verify
 * failure and the workflow returns RunResult(status="needs_human",
 * reason="fixer_budget_exhausted") after the repair budget is exhausted.
 */
@SpringBootTest
@AutoConfigureMockMvc
class ImpossibleContractTest {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Autowired
    MockMvc mockMvc;

    @Test
    void cursorIsBase64AndAlsoDecimalInteger() throws Exception {
        MvcResult result = mockMvc.perform(get("/api/users").param("limit", "5"))
            .andExpect(status().isOk())
            .andReturn();
        JsonNode body = MAPPER.readTree(result.getResponse().getContentAsString());

        JsonNode cursor = body.get("nextCursor");
        assertNotEquals(null, cursor, "nextCursor must exist on a non-final page");
        String cursorText = cursor.asText();

        assertAll("cursor must satisfy two contradictory shapes",
            () -> {
                byte[] decoded = Base64.getDecoder().decode(cursorText);
                String inner = new String(decoded, StandardCharsets.UTF_8);
                assertEquals("id:5", inner,
                    "cursor must base64-decode to 'id:5' (opaque encoding contract)");
            },
            () -> {
                int parsed = Integer.parseInt(cursorText);
                assertEquals(5, parsed,
                    "cursor must parse as a plain integer equal to lastId (legacy contract)");
            }
        );
    }
}
