package com.example.users;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/users")
public class UserController {
    private final UserRepository repository;

    public UserController(UserRepository repository) {
        this.repository = repository;
    }

    @GetMapping
    public Map<String, Object> list(
        @RequestParam(defaultValue = "0") int offset,
        @RequestParam(defaultValue = "10") int limit
    ) {
        List<User> page = repository.findAll(offset, limit);
        return Map.of(
            "items", page,
            "offset", offset,
            "limit", limit,
            "total", repository.total()
        );
    }
}
