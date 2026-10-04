const std = @import("std");
const store = @import("store.zig");

const Item = struct {
    id: u32,
};

pub fn main() !void {
    const item = store.load(1);
    std.debug.print("{}", .{item.id});
}

fn helper() u32 {
    return 1;
}
