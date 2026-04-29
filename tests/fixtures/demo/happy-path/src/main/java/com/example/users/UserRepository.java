package com.example.users;

import org.springframework.stereotype.Repository;

import java.util.ArrayList;
import java.util.List;
import java.util.stream.IntStream;

@Repository
public class UserRepository {
    private final List<User> users = new ArrayList<>();

    public UserRepository() {
        IntStream.rangeClosed(1, 50).forEach(i ->
            users.add(new User(i, "User " + i, "user" + i + "@example.com"))
        );
    }

    public List<User> findAll(int offset, int limit) {
        if (offset >= users.size()) {
            return List.of();
        }
        int end = Math.min(offset + limit, users.size());
        return List.copyOf(users.subList(offset, end));
    }

    public int total() {
        return users.size();
    }
}
