up:
	docker compose up -d --build

update:
	./scripts/commit-and-push.sh
	git pull --rebase
	$(MAKE) up

logs:
	docker compose logs -f hamroh

down:
	docker compose down
