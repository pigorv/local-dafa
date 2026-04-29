package com.example.users;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

@SpringBootTest
@AutoConfigureMockMvc
class UserControllerTest {

    @Autowired
    MockMvc mockMvc;

    @Test
    void offsetPaginationStillWorks() throws Exception {
        mockMvc.perform(get("/api/users").param("offset", "0").param("limit", "5"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.items.length()").value(5))
            .andExpect(jsonPath("$.items[0].id").value(1));
    }
}
