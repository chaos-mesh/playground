package playground;

import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.lang.management.ManagementFactory;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;

public class JvmProbe {
    public static void main(String[] args) throws IOException {
        HttpServer server = HttpServer.create(new InetSocketAddress(8080), 0);
        server.createContext("/healthz", exchange -> {
            exchange.sendResponseHeaders(204, -1);
            exchange.close();
        });
        server.createContext("/metrics", exchange -> {
            double value = Double.NaN;
            boolean failed = false;
            long started = System.nanoTime();
            try {
                value = probeValue();
            } catch (IllegalStateException exception) {
                failed = true;
            }
            double latencyMillis = (System.nanoTime() - started) / 1_000_000.0;
            long collections = 0;
            long collectionMillis = 0;
            for (var collector : ManagementFactory.getGarbageCollectorMXBeans()) {
                collections += Math.max(0, collector.getCollectionCount());
                collectionMillis += Math.max(0, collector.getCollectionTime());
            }
            var operatingSystem = (com.sun.management.OperatingSystemMXBean)
                    ManagementFactory.getOperatingSystemMXBean();
            byte[] body = ("playground_jvm_method_return_value " + value + "\n"
                    + "playground_jvm_method_latency_milliseconds " + latencyMillis + "\n"
                    + "playground_jvm_method_failed " + (failed ? 1 : 0) + "\n"
                    + "playground_jvm_rule_return_value{input=\"7\"} " + ruleValue(7) + "\n"
                    + "playground_jvm_rule_return_value{input=\"8\"} " + ruleValue(8) + "\n"
                    + "# TYPE playground_jvm_gc_collections_total counter\n"
                    + "playground_jvm_gc_collections_total " + collections + "\n"
                    + "# TYPE playground_jvm_gc_collection_seconds_total counter\n"
                    + "playground_jvm_gc_collection_seconds_total " + collectionMillis / 1000.0 + "\n"
                    + "# TYPE playground_jvm_cpu_seconds_total counter\n"
                    + "playground_jvm_cpu_seconds_total " + operatingSystem.getProcessCpuTime() / 1_000_000_000.0 + "\n")
                    .getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "text/plain; version=0.0.4");
            exchange.sendResponseHeaders(200, body.length);
            try (var response = exchange.getResponseBody()) {
                response.write(body);
            }
        });
        server.start();
    }

    static int ruleValue(int input) {
        return input;
    }

    static int probeValue() {
        return 7;
    }
}
