const core = require("@actions/core");
const github = require("@actions/github");
const fs = require("fs");
const { resolve } = require("path");
const simpleGit = require("simple-git");
const isBinaryFileSync = require("isbinaryfile").isBinaryFileSync;
const process = require("process");

{
	let eventName = github.context.eventName;
	if (eventName.startsWith("pull_request")) {
		console.log(`Event name: ${eventName}. There's nothing here yet.`);
		return;
	}
}

const githubToken = process.argv[2];
const jobType = process.argv[3];

const githubAccess = `https://x-access-token:${githubToken}@github.com/`;

class UserAgent {
	constructor() {
		let numberRegExp = "[0-9]+.[0-9]+.[0-9]+.[0-9]+";
		this.userAgentRegExp = new RegExp("Chrome/" + numberRegExp, "g");

		let versionRegExp = new RegExp(numberRegExp, "g");
		let fullVersion = require("child_process")
			.execSync("google-chrome --version");

		this.version = versionRegExp.exec(fullVersion);
		if (!this.version) {
			process.exit(1);
		}
		console.log(`Current version: ${this.version}.`);
	}

	replace(stringData) {
		return stringData.replace(
			this.userAgentRegExp,
			`Chrome/${this.version}`);
	}

	commitMessage() {
		return `Update User-Agent for DNS to Chrome ${this.version}.`;
	}

	branchName() {
		return `chrome_${this.version}`;
	}
};

class LicenseYear {
	constructor() {
		let d = new Date();
		this.year = d.getFullYear();
	}

	replace(stringData) {
		let previousYear = this.year - 1;
		let pattern = `copyright (c) 2014-${previousYear}`;
		return (stringData.toLowerCase().indexOf(pattern) >= 0)
			? stringData.replace(`2014-${previousYear}`, `2014-${this.year}`)
			: stringData;
	}

	commitMessage() {
		return `Update copyright year to ${this.year}.`;
	}

	branchName() {
		return `copyright_to_${this.year}`;
	}
};

const updater = (() => {
	return (jobType == "license-year")
		? new LicenseYear()
		: (jobType == "user-agent")
		? new UserAgent()
		: undefined;
})();

if (!updater) {
	console.log("Job type not found.");
	return;
}

//////

const cloneGit = info => {
	return simpleGit().clone(info.githubRepo)
		.then(() => (new Promise((good, bad) => { good(info); })));
};

const processFiles = info => {
	const readFiles = async dir => {
		const direntsOptions = { withFileTypes: true };
		const dirents = await fs.promises.readdir(dir, direntsOptions);
		const files = await Promise.all(dirents.map(dirent => {
			const res = resolve(dir, dirent.name);
			return dirent.isDirectory() ? readFiles(res) : res;
		}));
		return files.flat().filter(p => (p.indexOf(".git") == -1))
	};

	return readFiles(info.common.repo).then(paths => {
		let modifiedCount = 0;
		paths.forEach(path => {
			const bytes = fs.readFileSync(path);
			const size = fs.lstatSync(path).size;
			if (isBinaryFileSync(bytes, size)) {
				return;
			}
			const original = bytes.toString();
			const modified = updater.replace(original);
			if (original != modified) {
				if (modifiedCount == 0) {
					console.log("Modified files:");
				}
				console.log(path);
				fs.writeFileSync(path, modified);
				modifiedCount++;
			}
		});
		return new Promise((good, bad) => {
			if (modifiedCount == 0) {
				bad("No modified files.");
			} else {
				good(info);
			}
		});
	});
};

const processGit = info => {
	const git = simpleGit(info.common.repo);
	const url = info.githubRepo;
	const commit = updater.commitMessage();
	const branch = updater.branchName();

	return new Promise((good, bad) => {
		git.branch().then(branches => {
			info.baseBranch = branches.current;

			if (!branches.all.every((b) => (!b.includes(branch)))) {
				bad("Our branch already exists.");
				return;
			}

			git.remote(["set-url", "origin", url])
			.addConfig("user.name", "GitHub Action")
			.addConfig("user.email", "action@github.com")
			.checkoutLocalBranch(branch)
			.add(".")
			.commit(commit)
			.log((err, log) => {
				if (log.latest.message == commit) {
					git.push(["-u", "origin", branch]).then(() => {
						console.log(`Commit message: ${log.latest.message}`);
						good(info);
					});
				} else {
					bad();
				}
			});
		});
	});
};

const processPullRequest = info => {
	const octokit = github.getOctokit(githubToken);
	octokit.pulls.list(info.common).then(response => {
		if (response.data.every(i => (i.head.ref != updater.branchName()))) {
			octokit.pulls.create({
				title: updater.commitMessage(),
				body: "",
				head: updater.branchName(),
				base: info.baseBranch,
				...info.common
			}).then(r => {
				console.log("Pull request is created.");
			}).catch(console.error);
		} else {
			console.log("Pull request with this branch already exists.");
		}
	});
};

const createInfo = (owner, repo) => ({
	common: {
		owner: owner,
		repo: repo,
	},
	githubRepo: `${githubAccess}/${owner}/${repo}`,
	baseBranch: "",
});

const processJob = info => {
	cloneGit(info)
	.then(processFiles)
	.then(processGit)
	.then(processPullRequest)
	.catch(console.error);
};

processJob(createInfo(github.context.repo.owner, github.context.repo.repo));
